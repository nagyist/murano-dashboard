#    Copyright (c) 2013 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
import json
from django import forms
from django.core.validators import RegexValidator, validate_ipv4_address
from netaddr import all_matching_cidrs
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import smart_text
from muranodashboard.environments import api
from horizon import exceptions, messages
from openstack_dashboard.api import glance
from openstack_dashboard.api.nova import novaclient
from django.template.defaultfilters import pluralize
import copy
import types
import logging
import itertools
import horizon.tables as tables
import floppyforms
from django.template.loader import render_to_string

log = logging.getLogger(__name__)


def with_request(func):
    """The decorator is meant to be used together with `UpdatableFieldsForm':
    apply it to the `update' method of fields inside that form.
    """
    def update(self, initial, request=None, **kwargs):
        initial_request = initial.get('request')
        if initial_request:
            log.debug("Using 'request' value from initial dictionary")
            func(self, initial_request, **kwargs)
        elif request:
            log.debug("Using direct 'request' value")
            func(self, request, **kwargs)
        else:
            log.error("No 'request' value passed neither via initial "
                      "dictionary, nor directly")
            raise forms.ValidationError("Can't get a request information")
    return update


class CustomPropertiesField(forms.Field):
    def clean(self, value):
        """Skip all validators if field is disabled."""
        if getattr(self, 'enabled', True):
            return super(CustomPropertiesField, self).clean(value)
        else:
            return super(CustomPropertiesField, self).to_python(value)

    @classmethod
    def push_properties(cls, kwargs):
        props = {}
        for key, value in kwargs.iteritems():
            if isinstance(value, property):
                props[key] = value
        for key in props.keys():
            del kwargs[key]
        if props:
            return type('cls_with_props', (cls,), props)
        else:
            return cls


class CharField(forms.CharField, CustomPropertiesField):
    pass


class PasswordField(CharField):
    special_characters = '!@#$%^&*()_+|\/.,~?><:{}'
    password_re = re.compile('^.*(?=.*\d)(?=.*[a-z])(?=.*[A-Z])(?=.*[%s]).*$'
                             % special_characters)
    has_clone = False
    validate_password = RegexValidator(
        password_re, _('The password must contain at least one letter, one   \
                               number and one special character'), 'invalid')

    @staticmethod
    def get_clone_name(name):
        return name + '-clone'

    def compare(self, name, form_data):
        if self.is_original() and self.required:
            # run compare only for original fields
            # do not run compare for hidden fields (they are not required)
            if form_data.get(name) != form_data.get(self.get_clone_name(name)):
                raise forms.ValidationError(_(u"{0}{1} don't match".format(
                    self.label, pluralize(2))))

    class PasswordInput(forms.PasswordInput):
        class Media:
            js = ('muranodashboard/js/passwordfield.js',)

    def __init__(self, label, *args, **kwargs):
        help_text = kwargs.get('help_text')
        if not help_text:
            help_text = _('Enter a complex password with at least one letter, \
                one number and one special character')

        error_messages = {
            'invalid': self.validate_password.message}
        err_msg = kwargs.get('error_messages')
        if err_msg:
            if err_msg.get('required'):
                error_messages['required'] = err_msg.get('required')

        super(PasswordField, self).__init__(
            min_length=7,
            max_length=255,
            validators=[self.validate_password],
            label=label,
            error_messages=error_messages,
            help_text=help_text,
            widget=self.PasswordInput(render_value=True))

    def __deepcopy__(self, memo):
        result = super(PasswordField, self).__deepcopy__(memo)
        result.error_messages = copy.deepcopy(self.error_messages)
        return result

    def is_original(self):
        return hasattr(self, 'original') and self.original

    def clone_field(self):
        self.has_clone = True
        field = copy.deepcopy(self)
        self.original = True
        field.label = _('Confirm password')
        field.error_messages['required'] = _('Please confirm your password')
        field.help_text = _('Retype your password')
        return field


class IntegerField(forms.IntegerField, CustomPropertiesField):
    pass


class InstanceCountField(IntegerField):
    def clean(self, value):
        self.value = super(InstanceCountField, self).clean(value)
        return self.value

    def postclean(self, form, data):
        value = []
        if hasattr(self, 'value'):
            templates = form.get_unit_templates(data)
            for dc in range(self.value):
                if dc < len(templates) - 1:
                    template = templates[dc]
                else:
                    template = templates[-1]
                value.append(self.interpolate_number(template, dc + 1))
            return value

    @staticmethod
    def interpolate_number(spec, number):
        """Replaces all '#' occurrences with given number."""
        def interpolate(spec):
            if isinstance(spec, types.DictType):
                return dict((k, interpolate(v)) for (k, v) in spec.iteritems())
            elif isinstance(spec, basestring) and '#' in spec:
                return spec.replace('#', '{0}').format(number)
            else:
                return spec
        return interpolate(spec)


class Column(tables.Column):
    template_name = 'common/form-fields/data-grid/input.html'

    def __init__(self, transform, table_name=None, **kwargs):
        if hasattr(self, 'template_name'):
            def _transform(datum):
                context = {'data': getattr(datum, self.name, None),
                           'row_index': str(datum.id),
                           'table_name': table_name,
                           'column_name': self.name}
                return render_to_string(self.template_name, context)
            _transform.__name__ = transform
            transform = _transform
        super(Column, self).__init__(transform, **kwargs)


class CheckColumn(Column):
    template_name = 'common/form-fields/data-grid/checkbox.html'


class RadioColumn(Column):
    template_name = 'common/form-fields/data-grid/radio.html'


# FixME: we need to have separated object until find out way to use the same
# code for MS SQL Cluster datagrid
class Object(object):
    def __init__(self, id, **kwargs):
        self.id = id
        for key, value in kwargs.iteritems():
            setattr(self, key, value)

    def as_dict(self):
        item = {}
        for key, value in self.__dict__.iteritems():
            if key != 'id':
                item[key] = value
        return item


def DataTableFactory(name, columns):
    class Object(object):
        row_name_re = re.compile(r'.*\{0}.*')

        def __init__(self, id, **kwargs):
            self.id = id
            for key, value in kwargs.iteritems():
                if isinstance(value, basestring) and \
                        re.match(self.row_name_re, value):
                    setattr(self, key, value.format(id))
                else:
                    setattr(self, key, value)

    class DataTableBase(tables.DataTable):
        def __init__(self, request, data, **kwargs):
            if len(data) and isinstance(data[0], dict):
                objects = [Object(i, **item)
                           for (i, item) in enumerate(data, 1)]
            else:
                objects = data
            super(DataTableBase, self).__init__(request, objects, **kwargs)

    class Meta:
        template = 'common/form-fields/data-grid/data_table.html'
        name = ''
        footer = False

    attrs = dict((col_id, cls(col_id, verbose_name=col_name, table_name=name))
                 for (col_id, cls, col_name) in columns)
    attrs['Meta'] = Meta
    return tables.base.DataTableMetaclass('DataTable', (DataTableBase,), attrs)


class TableWidget(floppyforms.widgets.Input):
    template_name = 'common/form-fields/data-grid/table_field.html'
    delimiter_re = re.compile('([\w-]*)@@([0-9]*)@@([\w-]*)')
    types = {'label': Column,
             'radio': RadioColumn,
             'checkbox': CheckColumn}

    def __init__(self, columns_spec=None, table_class=None, js_buttons=True,
                 *args, **kwargs):
        assert columns_spec is not None or table_class is not None
        self.columns = []
        if columns_spec:
            for spec in columns_spec:
                name = spec['column_name']
                self.columns.append((name,
                                     self.types[spec['column_type']],
                                     spec.get('title', None) or name.title()))
        self.table_class = table_class
        self.js_buttons = js_buttons
        ignorable = kwargs.pop('widget', None)
        super(TableWidget, self).__init__(*args, **kwargs)

    def get_context(self, name, value, attrs=None):
        ctx = super(TableWidget, self).get_context_data()
        if value:
            if self.table_class:
                cls = self.table_class
            else:
                cls = DataTableFactory(name, self.columns)
            ctx['data_table'] = cls(self.request, value)
            ctx['js_buttons'] = self.js_buttons
        return ctx

    def value_from_datadict(self, data, files, name):
        def extract_value(row_key, col_id, col_cls):
            if col_cls == CheckColumn:
                val = data.get("{0}@@{1}@@{2}".format(name, row_key, col_id),
                               False)
                return val and val == 'on'
            elif col_cls == RadioColumn:
                row_id = data.get("{0}@@@@{1}".format(name, col_id), False)
                if row_id:
                    return int(row_id) == row_key
                return False
            else:
                return data.get("{0}@@{1}@@{2}".format(
                    name, row_key, col_id), None)

        def extract_keys():
            keys = set()
            for key in data.iterkeys():
                match = re.match(r'^[^@]+@@([^@]*)@@.*$', key)
                if match and match.group(1):
                    keys.add(match.group(1))
            return keys

        items = []
        if self.table_class:
            columns = [(_name, column.__class__, unicode(column.verbose_name))
                       for (_name, column)
                       in self.table_class.base_columns.items()]
        else:
            columns = self.columns

        for row_key in extract_keys():
            item = {}
            for column_id, column_instance, column_name in columns:
                value = extract_value(row_key, column_id, column_instance)
                item[column_id] = value
            items.append(Object(row_key, **item))

        return items

    class Media:
        css = {'all': ('muranodashboard/css/tablefield.css',)}
        js = ('muranodashboard/js/tablefield.js',)


class TableField(CustomPropertiesField):
    def __init__(self, columns=None, label=None, table_class=None, **kwargs):
        widget = TableWidget(columns, table_class, **kwargs)
        super(TableField, self).__init__(label=label, widget=widget)

    @with_request
    def update(self, request, **kwargs):
        self.widget.request = request

    def clean(self, objects):
        return [obj.as_dict() for obj in objects]


class ChoiceField(forms.ChoiceField, CustomPropertiesField):
    pass


class DomainChoiceField(ChoiceField):
    @with_request
    def update(self, request, **kwargs):
        self.choices = [("", "Not in domain")]
        link = request.__dict__['META']['HTTP_REFERER']
        environment_id = re.search(
            'murano/(\w+)', link).group(0)[7:]
        domains = api.service_list_by_type(request, environment_id,
                                           'activeDirectory')
        self.choices.extend(
            [(domain.name, domain.name) for domain in domains])


class FlavorChoiceField(ChoiceField):
    @with_request
    def update(self, request, **kwargs):
        self.choices = [(flavor.name, flavor.name) for flavor in
                        novaclient(request).flavors.list()]
        for flavor in self.choices:
            if 'medium' in flavor[1]:
                self.initial = flavor[0]
                break


class ImageChoiceField(ChoiceField):
    def __init__(self, *args, **kwargs):
        self.image_type = kwargs.pop('image_type', None)
        super(ImageChoiceField, self).__init__(*args, **kwargs)

    @with_request
    def update(self, request, **kwargs):
        try:
            # public filter removed
            images, _more = glance.image_list_detailed(request)
        except:
            log.error("Error to request image list from glance ")
            images = []
            exceptions.handle(request, _("Unable to retrieve public images."))

        image_mapping, image_choices = {}, []
        for image in images:
            murano_property = image.properties.get('murano_image_info')
            if murano_property:
                try:
                    murano_json = json.loads(murano_property)
                except ValueError:
                    log.warning("JSON in image metadata is not valid. "
                                "Check it in glance.")
                    messages.error(request, _("Invalid murano image metadata"))
                else:
                    title = murano_json.get('title', image.name)
                    murano_json['name'] = image.name

                    if self.image_type is not None:
                        itype = murano_json.get('type')

                        if not self.image_type and itype is None:
                            continue

                        prefix = '{type}.'.format(type=self.image_type)
                        if (not itype.startswith(prefix) and
                                not self.image_type == itype):
                            continue

                    image_mapping[smart_text(title)] = json.dumps(murano_json)

        for name in sorted(image_mapping.keys()):
            image_choices.append((image_mapping[name], name))
        if image_choices:
            image_choices.insert(0, ("", _("Select Image")))
        else:
            image_choices.insert(0, ("", _("No images available")))

        self.choices = image_choices

    def clean(self, value):
        value = super(ImageChoiceField, self).clean(value)
        return json.loads(value) if value else value


class AZoneChoiceField(ChoiceField):
    @with_request
    def update(self, request, **kwargs):
        try:
            availability_zones = novaclient(request).availability_zones.\
                list(detailed=False)
        except:
            availability_zones = []
            exceptions.handle(request,
                              _("Unable to retrieve  availability zones."))

        az_choices = [(az.zoneName, az.zoneName)
                      for az in availability_zones if az.zoneState]
        if not az_choices:
            az_choices.insert(0, ("", _("No availability zones available")))

        self.choices = az_choices


class BooleanField(forms.BooleanField, CustomPropertiesField):
    def __init__(self, *args, **kwargs):
        if 'widget' in kwargs:
            widget = kwargs['widget']
            if isinstance(widget, type):
                widget = widget(attrs={'class': 'checkbox'})
        else:
            widget = forms.CheckboxInput(attrs={'class': 'checkbox'})
        kwargs['widget'] = widget
        super(BooleanField, self).__init__(*args, **kwargs)


class ClusterIPField(CharField):
    @staticmethod
    def validate_cluster_ip(request, ip_ranges):
        def perform_checking(ip):
            validate_ipv4_address(ip)
            if not all_matching_cidrs(ip, ip_ranges) and ip_ranges:
                raise forms.ValidationError(_('Specified Cluster Static IP is'
                                              ' not in valid IP range'))
            try:
                ip_info = novaclient(request).fixed_ips.get(ip)
            except exceptions.UNAUTHORIZED:
                log.error("Error to get information about IP address"
                          " using novaclient")
                exceptions.handle(
                    request, _("Unable to retrieve information "
                               "about fixed IP or IP is not valid."),
                    ignore=True)
            except exceptions.NOT_FOUND:
                msg = "Could not found fixed ips for ip %s" % (ip,)
                log.error(msg)
                exceptions.handle(
                    request, _(msg),
                    ignore=True)
            else:
                if ip_info.hostname:
                    raise forms.ValidationError(
                        _('Specified Cluster Static IP is already in use'))
        return perform_checking

    @with_request
    def update(self, request, **kwargs):
        try:
            network_list = novaclient(request).networks.list()
            ip_ranges = [network.cidr for network in network_list]
            ranges = ', '.join(ip_ranges)
        except StandardError:
            ip_ranges, ranges = [], ''
        if ip_ranges:
            self.help_text = _('Select IP from available range: ' + ranges)
        else:
            self.help_text = _('Specify valid fixed IP')
        self.validators = [self.validate_cluster_ip(request, ip_ranges)]
        self.error_messages['invalid'] = validate_ipv4_address.message


class DatabaseListField(CharField):
    validate_mssql_identifier = RegexValidator(
        re.compile(r'^[a-zA-z_][a-zA-Z0-9_$#@]*$'),
        _((u'First symbol should be latin letter or underscore. Subsequent ' +
           u'symbols can be latin letter, numeric, underscore, at sign, ' +
           u'number sign or dollar sign')))

    default_error_messages = {'invalid': validate_mssql_identifier.message}

    def to_python(self, value):
        """Normalize data to a list of strings."""
        if not value:
            return []
        return [name.strip() for name in value.split(',')]

    def validate(self, value):
        """Check if value consists only of valid names."""
        super(DatabaseListField, self).validate(value)
        for db_name in value:
            self.validate_mssql_identifier(db_name)