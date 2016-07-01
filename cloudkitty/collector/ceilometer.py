# -*- coding: utf-8 -*-
# Copyright 2014 Objectif Libre
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
#
# @author: Stéphane Albert
#
from ceilometerclient import client as cclient
from oslo.config import cfg

from cloudkitty import collector
from cloudkitty import utils as ck_utils
from oslo_log import log as logging

ceilometer_collector_opts = [
    cfg.StrOpt('username',
               default='cloudkitty',
               help='OpenStack username.'),
    cfg.StrOpt('password',
               default='CK_PASSWORD',
               help='OpenStack password.'),
    cfg.StrOpt('tenant',
               default='service',
               help='OpenStack tenant.'),
    cfg.StrOpt('region',
               default='RegionOne',
               help='OpenStack region.'),
    cfg.StrOpt('url',
               default='http://127.0.0.1:5000',
               help='OpenStack auth URL.'), ]

cfg.CONF.register_opts(ceilometer_collector_opts, 'ceilometer_collector')
LOG = logging.getLogger(__name__)

class ResourceNotFound(Exception):
    """Raised when the resource doesn't exist."""

    def __init__(self, resource_type, resource_id):
        super(ResourceNotFound, self).__init__(
            "No such resource: %s, type: %s" % (resource_id, resource_type))
        self.resource_id = resource_id
        self.resource_type = resource_type


class CeilometerResourceCacher(object):
    def __init__(self):
        self._resource_cache = {}

    def add_resource_detail(self, resource_type, resource_id, resource_data):
        if resource_type not in self._resource_cache:
            self._resource_cache[resource_type] = {}
        self._resource_cache[resource_type][resource_id] = resource_data
        return self._resource_cache[resource_type][resource_id]

    def has_resource_detail(self, resource_type, resource_id):
        if resource_type in self._resource_cache:
            if resource_id in self._resource_cache[resource_type] and self._resource_cache[resource_type][resource_id]['metadata']:
                return True
        return False

    def get_resource_detail(self, resource_type, resource_id):
        try:
            resource = self._resource_cache[resource_type][resource_id]
            return resource
        except KeyError:
            raise ResourceNotFound(resource_type, resource_id)


class CeilometerCollector(collector.BaseCollector):
    collector_name = 'ceilometer'
    dependencies = ('CeilometerTransformer',
                    'CloudKittyFormatTransformer')

    def __init__(self, transformers, **kwargs):
        super(CeilometerCollector, self).__init__(transformers, **kwargs)
        self.user = cfg.CONF.ceilometer_collector.username
        self.password = cfg.CONF.ceilometer_collector.password
        self.tenant = cfg.CONF.ceilometer_collector.tenant
        self.region = cfg.CONF.ceilometer_collector.region
        self.keystone_url = cfg.CONF.ceilometer_collector.url

        self.t_ceilometer = self.transformers['CeilometerTransformer']
        self.t_cloudkitty = self.transformers['CloudKittyFormatTransformer']

        self._cacher = CeilometerResourceCacher()

        self._conn = cclient.get_client('2',
                                        os_username=self.user,
                                        os_password=self.password,
                                        os_auth_url=self.keystone_url,
                                        os_tenant_name=self.tenant,
                                        os_region_name=self.region)

    def gen_filter(self, op='eq', **kwargs):
        """Generate ceilometer filter from kwargs."""
        q_filter = []
        for kwarg in kwargs:
            if kwarg =='tenant_id':
                q_filter.append({'field': 'resource_metadata.tenant_id', 'op': op, 'value': kwargs[kwarg]})
            else:
                q_filter.append({'field': kwarg, 'op': op, 'value': kwargs[kwarg]})
        return q_filter


    def prepend_filter(self, prepend, **kwargs):
        """Filter composer."""
        q_filter = {}
        for kwarg in kwargs:
            q_filter[prepend + kwarg] = kwargs[kwarg]
        return q_filter

    def user_metadata_filter(self, op='eq', **kwargs):
        """Create user_metadata filter from kwargs."""
        user_filter = {}
        for kwarg in kwargs:
            field = kwarg
            # Auto replace of . to _ to match ceilometer behaviour
            if '.' in field:
                field = field.replace('.', '_')
            user_filter[field] = kwargs[kwarg]
        user_filter = self.prepend_filter('user_metadata.', **user_filter)
        return self.metadata_filter(op, **user_filter)

    def metadata_filter(self, op='eq', **kwargs):
        """Create metadata filter from kwargs."""
        meta_filter = self.prepend_filter('metadata.', **kwargs)
        return self.gen_filter(op, **meta_filter)

    def resources_stats(self,
                        meter,
                        start,
                        end=None,
                        project_id=None,
                        q_filter=None):
        """Resources statistics during the timespan."""
        start_iso = ck_utils.ts2iso(start)
        req_filter = self.gen_filter(op='ge', timestamp=start_iso)
        if project_id:
            if meter=='ip.floating':
                req_filter.extend(self.gen_filter(tenant_id=project_id))
            else:
                req_filter.extend(self.gen_filter(project=project_id))
        if end:
            end_iso = ck_utils.ts2iso(end)
            req_filter.extend(self.gen_filter(op='le', timestamp=end_iso))
        if isinstance(q_filter, list):
            req_filter.extend(q_filter)
        elif q_filter:
            req_filter.append(q_filter)
        resources_stats = self._conn.statistics.list(meter_name=meter,
                                                     period=0, q=req_filter,
                                                     groupby=['resource_id'])
        return resources_stats

    def active_resources(self,
                         meter,
                         start,
                         end=None,
                         project_id=None,
                         q_filter=None):
        """Resources that were active during the timespan."""
        resources_stats = self.resources_stats(meter,
                                               start,
                                               end,
                                               project_id,
                                               q_filter)
        return [resource.groupby['resource_id']
                for resource in resources_stats]

    def get_compute(self, start, end=None, project_id=None, q_filter=None):
        active_instance_ids = self.active_resources('instance', start, end,
                                                    project_id, q_filter)
        compute_data = []
        for instance_id in active_instance_ids:
            if not self._cacher.has_resource_detail('compute', instance_id):
                raw_resource = self._conn.resources.get(instance_id)
                instance = self.t_ceilometer.strip_resource_data('compute',
                                                                 raw_resource,instance_id)
                self._cacher.add_resource_detail('compute',
                                                 instance_id,
                                                 instance)
            instance = self._cacher.get_resource_detail('compute',
                                                        instance_id)
            compute_data.append(self.t_cloudkitty.format_item(instance,
                                                              'instance',
                                                              1))
        if not compute_data:
            raise collector.NoDataCollected(self.collector_name, 'compute')
        return self.t_cloudkitty.format_service('compute', compute_data)

    def get_image(self, start, end=None, project_id=None, q_filter=None):
        active_image_stats = self.resources_stats('image.size',
                                                  start,
                                                  end,
                                                  project_id,
                                                  q_filter)
        image_data = []
        for image_stats in active_image_stats:
            image_id = image_stats.groupby['resource_id']
            if not self._cacher.has_resource_detail('image', image_id):
                raw_resource = self._conn.resources.get(image_id)
                image = self.t_ceilometer.strip_resource_data('image',
                                                              raw_resource,image_id)
                self._cacher.add_resource_detail('image',
                                                 image_id,
                                                 image)
            image = self._cacher.get_resource_detail('image',
                                                     image_id)
            image_data.append(self.t_cloudkitty.format_item(image,
                                                            'image',
                                                            image_stats.max))
        if not image_data:
            raise collector.NoDataCollected(self.collector_name, 'image')
        return self.t_cloudkitty.format_service('image', image_data)

    def get_volume(self, start, end=None, project_id=None, q_filter=None):
        active_volume_ids = self.active_resources('volume',
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)
        volume_data = []
        for volume_id in active_volume_ids:
            if not self._cacher.has_resource_detail('volume',
                                                    volume_id):
                raw_resource = self._conn.resources.get(volume_id)
                volume = self.t_ceilometer.strip_resource_data('volume',
                                                               raw_resource,volume_id)
                self._cacher.add_resource_detail('volume',
                                                 volume_id,
                                                 volume)
            volume = self._cacher.get_resource_detail('volume',
                                                      volume_id)
            volume_data.append(self.t_cloudkitty.format_item(volume,
                                                             'volume',
                                                             1))
        if not volume_data:
            raise collector.NoDataCollected(self.collector_name, 'volume')
        return self.t_cloudkitty.format_service('volume', volume_data)

    def get_volume_size(self, start, end=None, project_id=None, q_filter=None):
        active_volume_stats = self.resources_stats('volume.size',
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)
        volume_data = []
        for volume_stats in active_volume_stats:
            volume_id = volume_stats.groupby['resource_id']
            if not self._cacher.has_resource_detail('volume.size',
                                                    volume_id):
                raw_resource = self._conn.resources.get(volume_id)
                volume = self.t_ceilometer.strip_resource_data('volume',
                                                               raw_resource,volume_id)
                self._cacher.add_resource_detail('volume.size',
                                                 volume_id,
                                                 volume)
            volume = self._cacher.get_resource_detail('volume.size',
                                                      volume_id)
            volume_data.append(self.t_cloudkitty.format_item(volume,
                                                             'GB',
                                                             volume_stats.max))
        if not volume_data:
            raise collector.NoDataCollected(self.collector_name, 'volume.size')
        return self.t_cloudkitty.format_service('volume.size', volume_data)

    def _get_network_bw(self,
                        direction,
                        start,
                        end=None,
                        project_id=None,
                        q_filter=None):
        if direction == 'in':
            resource_type = 'network.incoming.bytes'
        else:
            direction = 'out'
            resource_type = 'network.outgoing.bytes'
        active_tap_stats = self.resources_stats(resource_type,
                                                start,
                                                end,
                                                project_id,
                                                q_filter)
        bw_data = []
        for tap_stat in active_tap_stats:
            tap_id = tap_stat.groupby['resource_id']
            if not self._cacher.has_resource_detail('network.tap',
                                                    tap_id):
                raw_resource = self._conn.resources.get(tap_id)
                tap = self.t_ceilometer.strip_resource_data(
                    'network.tap',
                    raw_resource,tap_id)
                self._cacher.add_resource_detail('network.tap',
                                                 tap_id,
                                                 tap)
            tap = self._cacher.get_resource_detail('network.tap',
                                                   tap_id)
            tap_bw_mb = tap_stat.max / 1.0
            bw_data.append(self.t_cloudkitty.format_item(tap,
                                                         'B',
                                                         tap_bw_mb))
        ck_res_name = 'network.bw.{}'.format(direction)
        if not bw_data:
            raise collector.NoDataCollected(self.collector_name,
                                            ck_res_name)
        return self.t_cloudkitty.format_service(ck_res_name,
                                                bw_data)

    def get_network_bw_out(self,
                           start,
                           end=None,
                           project_id=None,
                           q_filter=None):
        return self._get_network_bw('out', start, end, project_id, q_filter)

    def get_network_bw_in(self,
                          start,
                          end=None,
                          project_id=None,
                          q_filter=None):
        return self._get_network_bw('in', start, end, project_id, q_filter)

    def get_network_floating(self,
                             start,
                             end=None,
                             project_id=None,
                             q_filter=None):
        active_floating_ids = self.active_resources('ip.floating',
                                                    start,
                                                    end,
                                                    project_id,
                                                    q_filter)
        floating_data = []
        for floating_id in active_floating_ids:
            if not self._cacher.has_resource_detail('network.floating',
                                                    floating_id):
                raw_resource = self._conn.resources.get(floating_id)
                floating = self.t_ceilometer.strip_resource_data(
                    'network.floating',
                    raw_resource,floating_id)
                self._cacher.add_resource_detail('network.floating',
                                                 floating_id,
                                                 floating)
            floating = self._cacher.get_resource_detail('network.floating',
                                                        floating_id)
            floating_data.append(self.t_cloudkitty.format_item(floating,
                                                               'ip',
                                                               1))
        if not floating_data:
            raise collector.NoDataCollected(self.collector_name,
                                            'network.floating')
        return self.t_cloudkitty.format_service('network.floating',
                                                floating_data)
### radosgw
    def get_radosgw_containers_objects_size(self, start, end=None, project_id=None, q_filter=None):
        active_radosgw_stats = self.resources_stats('radosgw.containers.objects.size',
                                                    start,
                                                    end,
                                                    project_id,
                                                    q_filter)
        radosgw_data = []
        for radosgw_stats in active_radosgw_stats:
            radosgw_id, radosgw_container = radosgw_stats.groupby['resource_id'].split('/')
            if not self._cacher.has_resource_detail('radosgw.containers.objects.size',
                                                    radosgw_container):
                raw_resource = self._conn.resources.get(radosgw_id)
                radosgw = self.t_ceilometer.strip_resource_data('radosgw.containers.objects.size',
                                                                raw_resource,radosgw_id)
                radosgw['container'] = radosgw_container
                self._cacher.add_resource_detail('radosgw.containers.objects.size',
                                                 radosgw_container,
                                                 radosgw)
            radosgw = self._cacher.get_resource_detail('radosgw.containers.objects.size',
                                                       radosgw_container)
            radosgw_size_mb = radosgw_stats.max / 1.0
            radosgw_data.append(self.t_cloudkitty.format_item(radosgw,
                                                              'B',
                                                              radosgw_size_mb))
        if not radosgw_data:
            raise collector.NoDataCollected(self.collector_name, 'radosgw.containers.objects.size')
        return self.t_cloudkitty.format_service('radosgw.containers.objects.size', radosgw_data)

    def get_radosgw_api_request(self, start, end=None, project_id=None, q_filter=None):
        active_radosgw_stats = self.resources_stats('radosgw.api.request',
                                                    start,
                                                    end,
                                                    project_id,
                                                    q_filter)
        radosgw_data = []
        for radosgw_stats in active_radosgw_stats:
            radosgw_id = radosgw_stats.groupby['resource_id']
            if not self._cacher.has_resource_detail('radosgw.api.request',
                                                    radosgw_id):
                raw_resource = self._conn.resources.get(radosgw_id)
                radosgw = self.t_ceilometer.strip_resource_data('radosgw.api.request',
                                                                raw_resource,radosgw_id)

                self._cacher.add_resource_detail('radosgw.api.request',
                                                 radosgw_id,
                                                 radosgw)
            radosgw = self._cacher.get_resource_detail('radosgw.api.request',
                                                       radosgw_id)

            radosgw_data.append(self.t_cloudkitty.format_item(radosgw,
                                                              'request',
                                                              radosgw_stats.sum))
        if not radosgw_data:
            raise collector.NoDataCollected(self.collector_name, 'radosgw.api.request')
        return self.t_cloudkitty.format_service('radosgw.api.request', radosgw_data)

    def _get_radosgw_bw(self,
                        direction,
                        start,
                        end=None,
                        project_id=None,
                        q_filter=None):

        if direction == 'external.in':
            resource_type = 'radosgw.external.incomming.byte'
        elif direction == 'external.out':
            resource_type = 'radosgw.external.outgoing.byte'
        elif direction == 'internal.in':
            resource_type = 'radosgw.internal.incomming.byte'
        elif direction == 'internal.out':
            resource_type = 'radosgw.internal.outgoing.byte'

        active_radosgw_stats = self.resources_stats(resource_type,
                                                    start,
                                                    end,
                                                    project_id,
                                                    q_filter)
        radosgw_data = []
        for radosgw_stats in active_radosgw_stats:
            radosgw_id = radosgw_stats.groupby['resource_id']


            if not self._cacher.has_resource_detail(resource_type,
                                                    radosgw_id):
                raw_resource = self._conn.resources.get(radosgw_id)

                radosgw = self.t_ceilometer.strip_resource_data(resource_type,
                                                                raw_resource,
                                                                radosgw_id)
                self._cacher.add_resource_detail(resource_type,
                                                 radosgw_id,
                                                 radosgw)
            radosgw = self._cacher.get_resource_detail(resource_type,
                                                       radosgw_id)
            radosgw_mb = radosgw_stats.sum / 1.0
            radosgw_data.append(self.t_cloudkitty.format_item(radosgw,
                                                              'B',
                                                              radosgw_mb))
        if not radosgw_data:
            raise collector.NoDataCollected(self.collector_name, resource_type)
        return self.t_cloudkitty.format_service(resource_type, radosgw_data)

    def get_radosgw_external_bw_in(self, start, end=None, project_id=None, q_filter=None):
        return self._get_radosgw_bw('external.in', start, end, project_id, q_filter)

    def get_radosgw_external_bw_out(self, start, end=None, project_id=None, q_filter=None):
        return self._get_radosgw_bw('external.out', start, end, project_id, q_filter)
### /radosgw

### lbs
    def _get_network_bw_lbs(self,
                        direction,
                        start,
                        end=None,
                        project_id=None,
                        q_filter=None):

        if direction == 'lbs.in':
            resource_type = 'network.services.lb.incoming.bytes'
        else:
            direction = 'lbs.out'
            resource_type = 'network.services.lb.outgoing.bytes'

        active_lbs_stats = self.resources_stats(resource_type,
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)

        old_active_lbs_stats = self.resources_stats(resource_type,
                                                   start-300,
                                                   start,
                                                   project_id,
                                                   q_filter)
        lbs_data = []
        for lbs_stats in active_lbs_stats:
            lbs_id = lbs_stats.groupby['resource_id']
            lbs_flow = lbs_stats.max

            for old_lbs_stats in old_active_lbs_stats:
                old_lbs_id = old_lbs_stats.groupby['resource_id']
                LOG.info("{old_lbs_id}:{lbs_id}".format(old_lbs_id=old_lbs_id, lbs_id=lbs_id))
                if lbs_id == old_lbs_id and lbs_stats.max > old_lbs_stats.max:
                    lbs_flow = lbs_stats.max-old_lbs_stats.max
                    break

            if not self._cacher.has_resource_detail('network.tap',
                                                    lbs_id):
                raw_resource = self._conn.resources.get(lbs_id)

                lbs = self.t_ceilometer.strip_resource_data('network.tap',
                                                               raw_resource,lbs_id)
                self._cacher.add_resource_detail('network.tap',
                                                 lbs_id,
                                                 lbs)
            lbs = self._cacher.get_resource_detail('network.tap',
                                                      lbs_id)

            lbs_bw_mb = lbs_flow / 1.0
            lbs_data.append(self.t_cloudkitty.format_item(lbs,
                                                             'B',
                                                             lbs_bw_mb))
        ck_res_name = 'network.bw.{}'.format(direction)
        if not lbs_data:
            raise collector.NoDataCollected(self.collector_name, ck_res_name)
        return self.t_cloudkitty.format_service(ck_res_name, lbs_data)


    def get_network_bw_lbs_in(self,
                          start,
                          end=None,
                          project_id=None,
                          q_filter=None):
        return self._get_network_bw_lbs('lbs.in', start, end, project_id, q_filter)

    def get_network_bw_lbs_out(self,
                          start,
                          end=None,
                          project_id=None,
                          q_filter=None):
        return self._get_network_bw_lbs('lbs.out', start, end, project_id, q_filter)

    def get_network_bw_lbs_pool(self, start, end=None, project_id=None, q_filter=None):
        active_lbs_pool_ids = self.active_resources('network.services.lb.pool',
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)
        lbs_pool_data = []

        for lbs_pool_id in active_lbs_pool_ids:


            if not self._cacher.has_resource_detail('network.bw.lbs.pool',
                                                    lbs_pool_id):
                raw_resource = self._conn.resources.get(lbs_pool_id)

                network_bw_lbs_pool = self.t_ceilometer.strip_resource_data('network.bw.lbs.pool',
                                                               raw_resource,lbs_pool_id)
                self._cacher.add_resource_detail('network.bw.lbs.pool',
                                                 lbs_pool_id,
                                                 network_bw_lbs_pool)
            network_bw_lbs_pool = self._cacher.get_resource_detail('network.bw.lbs.pool',
                                                      lbs_pool_id)
            lbs_pool_data.append(self.t_cloudkitty.format_item(network_bw_lbs_pool,
                                                             'pool',
                                                             1))
        if not lbs_pool_data:

            raise collector.NoDataCollected(self.collector_name, 'network.bw.lbs.pool')
        return self.t_cloudkitty.format_service('network.bw.lbs.pool', lbs_pool_data)

### /lbs
### bandwidth
    def get_bandwidth(self, start, end=None, project_id=None, q_filter=None):
        active_bandwidth_stats = self.resources_stats('bandwidth',
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)
        bandwidth_data = []
        for bandwidth_stats in active_bandwidth_stats:
            bandwidth_id = bandwidth_stats.groupby['resource_id']


            if not self._cacher.has_resource_detail('bandwidth',
                                                    bandwidth_id):
                raw_resource = self._conn.resources.get(bandwidth_id)

                network_bandwidth_pool = self.t_ceilometer.strip_resource_data('bandwidth',
                                                               raw_resource,bandwidth_id)
                self._cacher.add_resource_detail('bandwidth',
                                                 bandwidth_id,
                                                 network_bandwidth_pool)
            network_bandwidth_pool = self._cacher.get_resource_detail('bandwidth',
                                                      bandwidth_id)
            bandwidth_mb = bandwidth_stats.sum / 1.0
            bandwidth_data.append(self.t_cloudkitty.format_item(network_bandwidth_pool,
                                                             'B',
                                                             bandwidth_mb))
        if not bandwidth_data:
            raise collector.NoDataCollected(self.collector_name, 'bandwidth')
        return self.t_cloudkitty.format_service('bandwidth', bandwidth_data)
### /bandwidth
  
### snapshot
    def get_snapshot (self, start, end=None, project_id=None, q_filter=None):

        active_snapshot_ids = self.active_resources('snapshot', start, end,
                                                    project_id, q_filter)
        snapshot_data = []
        for snapshot_id in active_snapshot_ids:
            if not self._cacher.has_resource_detail('snapshot', snapshot_id):
                raw_resource = self._conn.resources.get(snapshot_id)
                snapshot = self.t_ceilometer.strip_resource_data('snapshot',
                                                                 raw_resource,snapshot_id)

                raw_resource_volume = self._conn.resources.get(raw_resource.metadata['volume_id'])
                snapshot['metadata']['volume_type'] = raw_resource_volume.metadata['volume_type']



                self._cacher.add_resource_detail('snapshot',
                                                 snapshot_id,
                                                 snapshot)
            snapshot = self._cacher.get_resource_detail('snapshot',
                                                        snapshot_id)
            snapshot_data.append(self.t_cloudkitty.format_item(snapshot,
                                                              'snapshot',
                                                              1))
        if not snapshot_data:
            raise collector.NoDataCollected(self.collector_name, 'snapshot')
        return self.t_cloudkitty.format_service('snapshot', snapshot_data)

    def get_snapshot_size (self, start, end=None, project_id=None, q_filter=None):

        active_snapshot_size_stats = self.resources_stats('snapshot.size',
                                                   start,
                                                   end,
                                                   project_id,
                                                   q_filter)
        snapshot_size_data = []
        for snapshot_size_stats in active_snapshot_size_stats:
            snapshot_size_id = snapshot_size_stats.groupby['resource_id']


            if not self._cacher.has_resource_detail('snapshot.size',
                                                    snapshot_size_id):
                raw_resource = self._conn.resources.get(snapshot_size_id)


                snapshot_size_pool = self.t_ceilometer.strip_resource_data('snapshot.size',
                                                               raw_resource,snapshot_size_id)
                raw_resource_volume = self._conn.resources.get(raw_resource.metadata['volume_id'])
                snapshot_size_pool['metadata']['volume_type'] = raw_resource_volume.metadata['volume_type']

                self._cacher.add_resource_detail('snapshot.size',
                                                 snapshot_size_id,
                                                 snapshot_size_pool)
            snapshot_size_pool = self._cacher.get_resource_detail('snapshot.size',
                                                      snapshot_size_id)
            snapshot_size_data.append(self.t_cloudkitty.format_item(snapshot_size_pool,
                                                             'GB',
                                                             snapshot_size_stats.max))
        if not snapshot_size_data:
            raise collector.NoDataCollected(self.collector_name, 'snapshot.size')
        return self.t_cloudkitty.format_service('snapshot.size', snapshot_size_data)
### /snapshot

