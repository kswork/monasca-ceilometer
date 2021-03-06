#
# Copyright 2015 Hewlett-Packard Company
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime
from oslo_config import cfg
from oslo_log import log
from oslo_utils import timeutils
import yaml


from ceilometer import sample as sample_util

OPTS = [
    cfg.StrOpt('monasca_mappings',
               default='/etc/ceilometer/monasca_field_definitions.yaml',
               help='Monasca static and dynamic field mappings'),
]
cfg.CONF.register_opts(OPTS, group='monasca')

MULTI_REGION_OPTS = [
    cfg.StrOpt('control_plane',
               default='None',
               help='The name of control plane'),
    cfg.StrOpt('cluster',
               default='None',
               help='The name of cluster'),
    cfg.StrOpt('cloud_name',
               default='None',
               help='The name of cloud')
]
cfg.CONF.register_opts(MULTI_REGION_OPTS)

LOG = log.getLogger(__name__)


class UnableToLoadMappings(Exception):
    pass


class NoMappingsFound(Exception):
    pass


class MonascaDataFilter(object):
    def __init__(self):
        self._mapping = {}
        self._mapping = self._get_mapping()

    def _get_mapping(self):
        with open(cfg.CONF.monasca.monasca_mappings, 'r') as f:
            try:
                return yaml.safe_load(f)
            except yaml.YAMLError as exc:
                raise UnableToLoadMappings(exc.message)

    def _convert_timestamp(self, timestamp):
        if isinstance(timestamp, datetime.datetime):
            ts = timestamp
        else:
            ts = timeutils.parse_isotime(timestamp)
        tdelta = (ts - datetime.datetime(1970, 1, 1, tzinfo=ts.tzinfo))
        # convert timestamp to milli seconds as Monasca expects
        return int(tdelta.total_seconds() * 1000)

    def _convert_to_sample(self, s):
        return sample_util.Sample(
            name=s['counter_name'],
            type=s['counter_type'],
            unit=s['counter_unit'],
            volume=s['counter_volume'],
            user_id=s['user_id'],
            project_id=s['project_id'],
            resource_id=s['resource_id'],
            timestamp=s['timestamp'],
            resource_metadata=s['resource_metadata'],
            source=s.get('source')).as_dict()

    def get_value_for_nested_dictionary(self, lst, dct):
        val = dct
        for element in lst:
            if isinstance(val, dict) and element in val:
                val = val.get(element)
            else:
                return
        return val

    def process_sample_for_monasca(self, sample_obj):
        if not self._mapping:
            raise NoMappingsFound("Unable to process the sample")

        dimensions = {}
        dimensions['datasource'] = 'ceilometer'
        # control_plane, cluster and cloud_name can be None, but we use
        # literal 'None' for such case
        dimensions['control_plane'] = cfg.CONF.control_plane or 'None'
        dimensions['cluster'] = cfg.CONF.cluster or 'None'
        dimensions['cloud_name'] = cfg.CONF.cloud_name or 'None'
        if isinstance(sample_obj, sample_util.Sample):
            sample = sample_obj.as_dict()
        elif isinstance(sample_obj, dict):
            if 'counter_name' in sample_obj:
                sample = self._convert_to_sample(sample_obj)
            else:
                sample = sample_obj

        sample_meta = sample.get('resource_metadata', None)

        for dim in self._mapping['dimensions']:
            val = sample.get(dim, None)
            if val is not None:
                dimensions[dim] = val
            else:
                dimensions[dim] = 'None'

        value_meta = {}
        meter_name = sample.get('name') or sample.get('counter_name')
        if sample_meta:
            for meta_key in self._mapping['metadata']['common']:
                val = sample_meta.get(meta_key, None)
                if val is not None:
                    value_meta[meta_key] = val
                else:
                    if len(meta_key.split('.')) > 1:
                        val = self.get_value_for_nested_dictionary(
                            meta_key.split('.'), sample_meta)
                        if val is not None:
                            value_meta[meta_key] = val
                        else:
                            value_meta[meta_key] = 'None'
                    else:
                        value_meta[meta_key] = 'None'

            if meter_name in self._mapping['metadata'].keys():
                for meta_key in self._mapping['metadata'][meter_name]:
                    val = sample_meta.get(meta_key, None)
                    if val is not None:
                        value_meta[meta_key] = val
                    else:
                        if len(meta_key.split('.')) > 1:
                            val = self.get_value_for_nested_dictionary(
                                meta_key.split('.'), sample_meta)
                            if val is not None:
                                value_meta[meta_key] = val
                            else:
                                value_meta[meta_key] = 'None'
                        else:
                            value_meta[meta_key] = 'None'

        meter_value = sample.get('volume') or sample.get('counter_volume')
        if meter_value is None:
            meter_value = 0

        metric = dict(
            name=meter_name,
            timestamp=self._convert_timestamp(sample['timestamp']),
            value=meter_value,
            dimensions=dimensions,
            value_meta=value_meta,
        )

        LOG.debug("Generated metric with name %(name)s,"
                  " timestamp %(timestamp)s, value %(value)s,"
                  " dimensions %(dimensions)s" %
                  {'name': metric['name'],
                   'timestamp': metric['timestamp'],
                   'value': metric['value'],
                   'dimensions': metric['dimensions']})

        return metric
