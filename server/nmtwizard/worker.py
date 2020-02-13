import time
import json
import logging
import six
import sys
import traceback
from nmtwizard import task
from nmtwizard import workeradmin
from nmtwizard.capacity import Capacity
from nmtwizard import config


def _compatible_resource(resource, request_resource):
    if request_resource == 'auto' or resource == request_resource:
        return True
    return (","+request_resource+",").find(","+resource+",") != -1


class Worker(object):

    def __init__(self, redis, services, ttl_policy, refresh_counter,
                 quarantine_time, worker_id, taskfile_dir,
                 default_config_timestamp=None):
        self._redis = redis
        self._service = next(iter(services))
        self._services = services
        self._logger = logging.getLogger('worker')
        self._worker_id = worker_id
        self._refresh_counter = refresh_counter
        self._quarantine_time = quarantine_time
        self._taskfile_dir = taskfile_dir
        self._default_config_timestamp = default_config_timestamp
        task.set_ttl_policy(ttl_policy)

    def run(self):
        self._logger.info('Starting worker')

        # Subscribe to beat expiration.
        pubsub = self._redis.pubsub()
        pubsub.psubscribe('__keyspace@0__:beat:*')
        pubsub.psubscribe('__keyspace@0__:queue:*')
        counter = 0
        counter_beat = 1000

        while True:
            counter_beat += 1
            # every 1000 * 0.01s (10s) - check & reset beat of the worker
            if counter_beat > 1000:
                counter_beat = 0
                if self._redis.exists(self._worker_id):
                    self._redis.hset(self._worker_id, "beat_time", time.time())
                    self._redis.expire(self._worker_id, 1200)
                else:
                    self._logger.info('stopped by key expiration/removal')
                    sys.exit(0)

            # every 100 * 0.01s (1s) - check worker administration command
            if counter_beat % 100 == 0:
                workeradmin.process(self._logger, self._redis, self._service)
                if (self._default_config_timestamp and
                        self._redis.hget('default', 'timestamp') != self._default_config_timestamp):
                    self._logger.info('stopped by default configuration change')
                    sys.exit(0)

            # process one message from the queue
            message = pubsub.get_message()
            if message:
                channel = message['channel']
                data = message['data']
                if data == 'expired':
                    # task expired, not beat was received
                    if channel.startswith('__keyspace@0__:beat:'):
                        task_id = channel[20:]
                        service = self._redis.hget('task:'+task_id, 'service')
                        if service in self._services:
                            self._logger.info('%s: task expired', task_id)
                            with self._redis.acquire_lock(task_id):
                                task.terminate(self._redis, task_id, phase='expired')
                    # expired in the queue - comes back in the work queue
                    elif channel.startswith('__keyspace@0__:queue:'):
                        task_id = channel[21:]
                        service = self._redis.hget('task:'+task_id, 'service')
                        if service in self._services:
                            self._logger.info('%s: move to work queue', task_id)
                            task.work_queue(self._redis, task_id, service)

            # process one element from work queue
            task_id = task.work_unqueue(self._redis, self._service)
            if task_id is not None:
                try:
                    self._advance_task(task_id)
                except RuntimeWarning:
                    self._logger.warning(
                        '%s: failed to acquire a lock, retrying', task_id)
                    task.work_queue(self._redis, task_id, self._service)
                except Exception as e:
                    self._logger.error('%s: %s', task_id, str(e))
                    with self._redis.acquire_lock(task_id):
                        task.set_log(self._redis, self._taskfile_dir, task_id, str(e))
                        task.terminate(self._redis, task_id, phase="launch_error")
                    self._logger.info(traceback.format_exc())
            # every 0.01s * refresh_counter - check if we can find some free resource
            if counter > self._refresh_counter:
                # if there are some queued tasks, look for free resources
                if self._redis.exists('queued:%s' % self._service):
                    self._logger.debug('checking processes on : %s', self._service)
                    self._select_best_task_to_process(self._services[self._service])
                counter = 0

            counter += 1
            time.sleep(0.01)

    def _advance_task(self, task_id):
        """Tries to advance the task to the next status. If it can, re-queue it immediately
        to process the next stage. Otherwise, re-queue it after some delay to try again.
        """
        keyt = 'task:%s' % task_id
        with self._redis.acquire_lock(keyt, acquire_timeout=1, expire_time=600):
            status = self._redis.hget(keyt, 'status')
            if status == 'stopped':
                return

            service_name = self._redis.hget(keyt, 'service')
            if service_name not in self._services:
                raise ValueError('unknown service %s' % service_name)
            service = self._services[service_name]

            self._logger.info('%s: trying to advance from status %s', task_id, status)

            if status == 'queued':
                resource = self._redis.hget(keyt, 'resource')
                parent = self._redis.hget(keyt, 'parent')
                if parent:
                    keyp = 'task:%s' % parent
                    # if the parent task is in the database, check for dependencies
                    if self._redis.exists(keyp):
                        status = self._redis.hget(keyp, 'status')
                        if status == 'stopped':
                            if self._redis.hget(keyp, 'message') != 'completed':
                                task.terminate(self._redis, task_id, phase='dependency_error')
                                return
                        else:
                            self._logger.warning('%s: depending on other task, waiting', task_id)
                            task.service_queue(self._redis, task_id, service.name)
                            return
                nxpus = Capacity(self._redis.hget(keyt, 'ngpus'), self._redis.hget(keyt, 'ncpus'))
                resource = self._allocate_resource(task_id, resource, service, nxpus)
                if resource is not None:
                    self._logger.info('%s: resource %s reserved %s',
                                      task_id, resource, nxpus)
                    self._redis.hset(keyt, 'alloc_resource', resource)
                    task.set_status(self._redis, keyt, 'allocated')
                    task.work_queue(self._redis, task_id, service_name)
                else:
                    self._logger.warning('%s: no resources available, waiting', task_id)
                    task.service_queue(self._redis, task_id, service.name)
            # elif status == 'allocating':
            #     resource = self._redis.hget(keyt, 'alloc_resource')
            #     nxpus = Capacity(self._redis.hget(keyt, 'ngpus'), self._redis.hget(keyt, 'ncpus'))
            #     already_allocated_xpus = Capacity()
            #     keygr = 'gpu_resource:%s:%s' % (service.name, resource)
            #     for k, v in six.iteritems(self._redis.hgetall(keygr)):
            #         if v == task_id:
            #             already_allocated_xpus.incr_ngpus(1)
            #     keycr = 'cpu_resource:%s:%s' % (service.name, resource)
            #     for k, v in six.iteritems(self._redis.hgetall(keycr)):
            #         if v == task_id:
            #             already_allocated_xpus.incr_ncpus(1)
            #     capacity = service.list_resources()[resource]
            #     available_xpus, remaining_xpus = self._reserve_resource(
            #                                         service, resource, capacity, task_id,
            #                                         nxpus - already_allocated_xpus,
            #                                         Capacity(), Capacity(-1, -1))
            #     self._logger.info(
            #         'task: %s - resource: %s (capacity %s)- already %s - available %s',
            #         task_id, resource, capacity, already_allocated_xpus, available_xpus)
            #     if available_xpus and available_xpus == nxpus - already_allocated_xpus:
            #         task.set_status(self._redis, keyt, 'allocated')
            #         key_reserved = 'reserved:%s:%s' % (service.name, resource)
            #         self._redis.delete(key_reserved)
            #         task.work_queue(self._redis, task_id, service.name)
            #     else:
            #         task.work_queue(self._redis, task_id, service.name,
            #                         delay=20)
            elif status == 'allocated':
                content = json.loads(self._redis.hget(keyt, 'content'))
                resource = self._redis.hget(keyt, 'alloc_resource')
                self._logger.info('%s: launching on %s', task_id, service.name)
                try:
                    entity_config = self._get_current_config(task_id)
                    keygr = 'gpu_resource:%s:%s' % (service.name, resource)
                    lgpu = []
                    for k, v in six.iteritems(self._redis.hgetall(keygr)):
                        if v == task_id:
                            lgpu.append(k)
                    self._redis.hset(keyt, 'alloc_lgpu', ",".join(lgpu))
                    keycr = 'cpu_resource:%s:%s' % (service.name, resource)
                    lcpu = []
                    for k, v in six.iteritems(self._redis.hgetall(keycr)):
                        if v == task_id:
                            lcpu.append(k)
                    self._redis.hset(keyt, 'alloc_lcpu', ",".join(lcpu))
                    data = service.launch(
                        task_id,
                        content['options'],
                        (lgpu, lcpu),
                        resource,
                        entity_config["storages"],
                        entity_config["docker"],
                        content['docker']['registry'],
                        content['docker']['image'],
                        content['docker']['tag'],
                        content['docker']['command'],
                        task.file_list(self._redis, self._taskfile_dir, task_id),
                        content['wait_after_launch'],
                        self._redis.hget(keyt, 'token'),
                        content.get('support_statistics'))
                except EnvironmentError as e:
                    # the resource is not available and will be set busy
                    self._block_resource(resource, service, str(e))
                    self._redis.hdel(keyt, 'alloc_resource')
                    # set the task as queued again
                    self._release_resource(service, resource, task_id,
                                           Capacity(self._redis.hget(keyt, 'ngpus'),
                                                    self._redis.hget(keyt, 'ncpus')))
                    task.set_status(self._redis, keyt, 'queued')
                    task.service_queue(self._redis, task_id, service.name)
                    self._logger.info('could not launch [%s] %s on %s: blocking resource',
                                      str(e), task_id, resource)
                    self._logger.info(traceback.format_exc())
                    return
                except Exception as e:
                    # all other errors make the task fail
                    self._logger.info('fail task [%s] - %s', task_id, str(e))
                    self._logger.info(traceback.format_exc())
                    task.append_log(self._redis, self._taskfile_dir, task_id, str(e))
                    task.terminate(self._redis, task_id, phase='launch_error')
                    self._logger.info(traceback.format_exc())
                    return
                self._logger.info('%s: task started on %s', task_id, service.name)
                self._redis.hset(keyt, 'job', json.dumps(data))
                task.set_status(self._redis, keyt, 'running')
                # For services that do not notify their activity, we should
                # poll the task status more regularly.
                task.work_queue(self._redis, task_id, service.name,
                                delay=service.is_notifying_activity and 120 or 30)

            elif status == 'running':
                self._logger.debug('- checking activity of task: %s', task_id)
                data = json.loads(self._redis.hget(keyt, 'job'))
                try:
                    status = service.status(task_id, data)
                except Exception as e:
                    self._logger.info('cannot get status for [%s] - %s', task_id, str(e))
                    self._redis.hincrby(keyt, 'status_fail', 1)
                    self._logger.info(traceback.format_exc())
                    if self._redis.hget(keyt, 'status_fail') > 4:
                        task.terminate(self._redis, task_id, phase='lost_connection')
                        return
                else:
                    self._redis.hdel(keyt, 'status_fail')
                if status == 'dead':
                    self._logger.info('%s: task no longer running on %s, request termination',
                                      task_id, service.name)
                    task.terminate(self._redis, task_id, phase='exited')
                else:
                    task.work_queue(self._redis, task_id, service.name,
                                    delay=service.is_notifying_activity and 600 or 120)

            elif status == 'terminating':
                data = self._redis.hget(keyt, 'job')
                nxpus = Capacity(self._redis.hget(keyt, 'ngpus'), self._redis.hget(keyt, 'ncpus'))
                if data is not None:
                    container_id = self._redis.hget(keyt, 'container_id')
                    data = json.loads(data)
                    data['container_id'] = container_id
                    self._logger.info('%s: terminating task (job: %s)', task_id, json.dumps(data))
                    try:
                        service.terminate(data)
                        self._logger.info('%s: terminated', task_id)
                    except Exception:
                        self._logger.warning('%s: failed to terminate', task_id)
                        self._logger.info(traceback.format_exc())
                else:
                    self._logger.info('%s: terminating task (on error)', task_id)
                resource = self._redis.hget(keyt, 'alloc_resource')
                if resource:
                    self._release_resource(service, resource, task_id, nxpus)
                task.set_status(self._redis, keyt, 'stopped')
                task.disable(self._redis, task_id)

    def _block_resource(self, resource, service, err):
        """Block a resource on which we could not launch a task
        """
        keyb = 'busy:%s:%s' % (service.name, resource)
        self._redis.set(keyb, err)
        self._redis.expire(keyb, self._quarantine_time)

    def _allocate_resource(self, task_id, request_resource, service, task_expected_capacity):
        """Allocates a resource for task_id and returns the name of the resource
           (or None if none where allocated), and the number of allocated gpus/cpus
        """
        best_resource = None
        br_remaining_xpus = Capacity(-1, -1)
        resources = service.list_resources()

        for name, capacity in six.iteritems(resources):
            if _compatible_resource(name, request_resource):
                better_remaining_xpus = self._reserve_resource(
                    service, name, capacity, task_id,
                    task_expected_capacity, br_remaining_xpus)
                if better_remaining_xpus is not None:
                    if best_resource is not None:
                        self._release_resource(service, best_resource, task_id, task_expected_capacity)
                    best_resource = name
                    br_remaining_xpus = better_remaining_xpus

        return best_resource

    def _reserve_resource(self, service, resource, capacity, task_id, task_asked_capacity, br_remaining_xpus):
        """Reserves the resource for task_id, if possible. The resource is locked
        while we try to reserve it.
        Resource should have more gpus available (within ngpus) than br_available_xpus
        or the same number but a smaller size
        """
        self._logger.debug('service.name = %s', service.name)
        self._logger.debug('resource = %s', resource)
        self._logger.debug('capacity = (%d, %d)', capacity.ngpus, capacity.ncpus)
        self._logger.debug('task_id = %s', task_id)
        self._logger.debug('nxpus = (%d, %d)', task_asked_capacity.ngpus, task_asked_capacity.ncpus)
        self._logger.debug('br_remaining_xpus = (%d, %d)', br_remaining_xpus.ngpus, br_remaining_xpus.ncpus)

        if capacity < task_asked_capacity:
            return False, False

        keygr = 'gpu_resource:%s:%s' % (service.name, resource)
        keycr = 'cpu_resource:%s:%s' % (service.name, resource)
        key_busy = 'busy:%s:%s' % (service.name, resource)

        with self._redis.acquire_lock(keygr):
            if self._redis.get(key_busy) is not None:
                return None
            # if we need gpus
            remaining_gpus = 0
            remaining_cpus = 0

            # allocate GPU first. For GPU we want to minimise the fragmentation, so minimize
            # br_remainining_xpus.ngpus

            current_usage_cpu = self._redis.hlen(keycr)
            self._logger.debug('current_usage_cpu = %d', current_usage_cpu)
            if current_usage_cpu > 0 and not service.resource_multitask:
                return None
            avail_cpu = capacity.ncpus - current_usage_cpu
            if task_asked_capacity.ncpus > avail_cpu:
                return None

            if task_asked_capacity.ngpus != 0:
                # do not allocate several run on the same GPU
                current_usage_gpu = self._redis.hlen(keygr)
                self._logger.debug('current_usage_gpu = %d', current_usage_gpu)
                if current_usage_gpu > 0 and not service.resource_multitask:
                    return None
                # available gpu is the capacity of the node less number of gpu used
                avail_gpu = capacity.ngpus - current_usage_gpu
                self._logger.debug('avail_gpu = %d', avail_gpu)

                if task_asked_capacity.ngpus > avail_gpu:
                    return None

                remaining_gpus = avail_gpu - task_asked_capacity.ngpus
                self._logger.debug('remaining_gpus = %d', remaining_gpus)

                if br_remaining_xpus.ngpus != -1 and remaining_gpus >= br_remaining_xpus.ngpus:
                    return None

            # if we don't need to allocate GPUs anymore, start allocating CPUs
            # * for CPU on multitask service we want to maximize the remaining CPU
            # to avoid loading too much individual servers
            # * for CPU on monotask service, we want to minimize the remaining CPU
            # to avoid loading on a over-dimensioned service
            # if allocated_gpu == task_asked_capacity.ngpus and task_asked_capacity.ncpus != 0:
            #     current_usage_cpu = self._redis.hlen(keycr)
            #     self._logger.debug('current_usage_cpu = %d', current_usage_cpu)
            #     if current_usage_cpu > 0 and not service.resource_multitask:
            #         return False, False
            #     avail_cpu = capacity.ncpus - current_usage_cpu
            #     self._logger.debug('avail_cpu = %d', avail_cpu)
            #     if  task_asked_capacity.ngpus.ncpus > avail_cpu:
            #         return False, False
            #     allocated_cpu = task_asked_capacity.ngpus.ncpus
            #     self._logger.debug('allocated_cpu = %d', allocated_cpu)
            remaining_cpus = avail_cpu - task_asked_capacity.ncpus
            self._logger.debug('remaining_cpus = %d', remaining_cpus)

            # for mono task service, allocate node with lowest cpu number
            if service.resource_multitask:
                better_cpu_usage = remaining_cpus > br_remaining_xpus.ncpus
            else:
                better_cpu_usage = remaining_cpus < br_remaining_xpus.ncpus

            if br_remaining_xpus.ncpus != -1 and not better_cpu_usage:
                return None

            idx = 1
            for i in xrange(task_asked_capacity.ngpus):
                while self._redis.hget(keygr, str(idx)) is not None:
                    idx += 1
                    assert idx <= capacity.ngpus, "invalid gpu alloc for %s" % keygr
                self._logger.debug('reserve GPU idx = %d', idx)
                self._redis.hset(keygr, str(idx), task_id)

            cpu_idx = 0
            for i in xrange(task_asked_capacity.ncpus):
                while self._redis.hget(keycr, str(cpu_idx)) is not None:
                    cpu_idx += 1
                    assert cpu_idx <= capacity.ncpus, "invalid cpu alloc for %s" % keycr
                self._logger.debug('reserve CPU idx = %d', cpu_idx)
                self._redis.hset(keycr, str(cpu_idx), task_id)

            return Capacity(remaining_gpus, remaining_cpus)

    def _release_resource(self, service, resource, task_id, nxpus):
        """remove the task from resource queue
        """
        self._logger.debug('releasing resource:%s on service: %s for %s %s',
                           resource, service.name, task_id, nxpus)
        if nxpus.ngpus != 0:
            keygr = 'gpu_resource:%s:%s' % (service.name, resource)
            with self._redis.acquire_lock(keygr):
                for k, v in six.iteritems(self._redis.hgetall(keygr)):
                    if v == task_id:
                        self._redis.hdel(keygr, k)
        if nxpus.ncpus != 0:
            keycr = 'cpu_resource:%s:%s' % (service.name, resource)
            with self._redis.acquire_lock(keycr):
                for k, v in six.iteritems(self._redis.hgetall(keycr)):
                    if v == task_id:
                        self._redis.hdel(keycr, k)

    def _select_best_task_to_process(self, service):
        """find the best next task to push to the work queue
        """
        class EntityUsage():
            def __init__(self, usage, entity_name, usage_rate_limit_capacitity):
                self._entity = entity_name
                self._current_usage_capactity = usage if usage else Capacity()
                self._usage_rate_limit_capacity = usage_rate_limit_capacitity
                self._task = []

            def add_current_usage(self, current_usage):
                if current_usage:
                    self._current_usage_capactity += current_usage

            def __le__(self, other_entity_usage):
                return self._current_usage_capactity <= other_entity_usage._current_usage_capactity

            def __lt__(self,other_entity_usage):
                return self._current_usage_capactity < other_entity_usage._current_usage_capactity

            def __eq__(self, other_entity_usage):
                return self._current_usage_capactity == other_entity_usage._current_usage_capactity

            def is_under_usage_limit(self):
                is_under_limit = self._current_usage_capactity < self._usage_rate_limit_capacity
                if not is_under_limit:
                    self._logger.info("\t[exceeded] entity %s - usage limit exceeded: %s.", self._entity, self._current_usage_capactity)
                return is_under_limit

            def add_task(self, task_id):
                self._task.append(task_id)

            @staticmethod
            def initialize_entities_usage(redis, service_name):
                entity_rate_lim = config.get_entities_limit_rate(redis, service_name)
                entities_usage = {e: EntityUsage(None, e, Capacity(r, r)) for e, r in six.iteritems(entity_rate_lim)}
                return entities_usage

        class CandidateTask():
            def __init__(self, task_id, task_entity, redis):
                assert task_id
                self._task_id = task_id
                self._entity = task_entity
                next_keyt = 'task:%s' % next_task_id
                self._priority = int(redis.hget(next_keyt, 'priority'))
                self._queued_time = float(redis.hget(next_keyt, 'queued_time'))

            @staticmethod
            def try_create(next_task_id):
                next_keyt = 'task:%s' % next_task_id
                parent = self._redis.hget(next_keyt, 'parent')
                task_entity = task.get_owner_entity(self._redis, next_task_id)

                if task_entity not in resource_mgr.entities_usage:
                    self._logger.error("\t[Task %s] entity %s - without usage limit !", next_task_id, task_entity)
                    return None

                # check parent dependency
                if parent:
                    keyp = 'task:%s' % parent
                    if self._redis.exists(keyp):
                        # if the parent task is in the database, check for dependencies
                        parent_status = self._redis.hget(keyp, 'status')
                        if parent_status != 'stopped':
                            if parent_status == 'running':
                                # parent is still running so update queued time to be as close
                                # as possible to terminate time of parent task
                                self._redis.hset(next_keyt, "queued_time", time.time())
                            return None
                        else:
                            if self._redis.hget(keyp, 'message') != 'completed':
                                task.terminate(self._redis, next_task_id,
                                               phase='dependency_error')
                                return None
                # task already allocated so, resource available
                found_resource = next_task_id in resource_mgr.preallocated_task_count
                if not found_resource: #  can the task be launched on any node ?
                    task_needed_capacity = Capacity(self._redis.hget(next_keyt, 'ngpus'), self._redis.hget(next_keyt, 'ncpus'))
                    found_resource = any(ResourceManager._is_authorized(srv, next_task_id, task_entity, task_needed_capacity)\
                                        and task_needed_capacity <= cap for srv, cap in six.iteritems(resource_mgr.avail_resource))

                if found_resource:
                    return CandidateTask(next_task_id, task_entity, self._redis)
                else:
                    return None

            def is_higher_priority(self, other_task):
                if not other_task:
                    return True

                is_more_prio =  self._priority > other_task._priority or (self._priority == other_task._priority and other_task._queued_time > self._queued_time)
                if self._entity == other_task._entity:  # same entity
                    return is_more_prio
                else:
                    entity_usage = resource_mgr.entities_usage[self.task_entity]
                    other_entity_usage = resource_mgr.entities_usage[other_task._entity]
                    #priority for under limit task
                    if other_entity_usage.is_under_usage_limit() and not entity_usage.is_under_usage_limit():
                        return False
                    elif not other_entity_usage.is_under_usage_limit() and entity_usage.is_under_usage_limit():
                        return True

                    if entity_usage < other_entity_usage:
                        return True
                    elif entity_usage > other_entity_usage:
                        return False
                    else:
                        return is_more_prio

        class ResourceManager():
            def __init__(self, worker):
                self.preallocated_task_count = {}
                self.preallocated_task_resource = {}
                self.avail_resource = {}
                self.resources = service.list_resources()
                self.entities_usage = {}
                self.worker = worker

            @staticmethod
            def _is_authorized(machine_name, task_id, task_entity, task_expected_capacity):
                only_entities = service.get_server_detail(machine_name, "entities")
                if only_entities and (task_entity not in only_entities or not only_entities[task_entity]):
                    self._logger.debug('%s excluded for %s, entity "%s"' % (machine_name, task_id, task_entity))
                    return False

                is_only_gpu_task = service.get_server_detail(machine_name, "only_gpu_task")
                if is_only_gpu_task is True and task_expected_capacity.ngpus <= 0:
                    self._logger.debug('%s excluded for %s' % (machine_name, task_id))
                    return False

                return True

            def exists_available_resource(self, service_name):
                self.resources = service.list_resources()
                self.entities_usage = EntityUsage.initialize_entities_usage(self.worker._redis, service_name)
                for resource in self.resources:
                    current_xpu_usage = Capacity()
                    capacity = self.resources[resource]
                    keygr = 'gpu_resource:%s:%s' % (self.worker._service, resource)
                    keycr = 'cpu_resource:%s:%s' % (self.worker._service, resource)

                    gpu_tasks = self.worker._redis.hgetall(keygr)
                    cpu_tasks = self.worker._redis.hgetall(keycr)

                    # can not launch multiple tasks on service with no multi-tasking (ec2)
                    if not service.resource_multitask and (gpu_tasks or cpu_tasks):
                        continue

                    for k, v in six.iteritems(gpu_tasks):
                        task_entity = task.get_owner_entity(self.worker._redis, v)
                        if v in self.preallocated_task_count:
                            self.preallocated_task_count[v].incr_ngpus(1)
                        else:
                            self.preallocated_task_count[v] = Capacity(ngpus=1)
                            self.preallocated_task_resource[v] = resource
                        current_xpu_usage.incr_ngpus(1)
                        self.entities_usage[task_entity].add_current_usage(Capacity(ngpus=1))

                    for k, v in six.iteritems(cpu_tasks):
                        task_entity = task.get_owner_entity(self.worker._redis, v)
                        if v in self.preallocated_task_count:
                            self.preallocated_task_count[v].incr_ncpus(1)
                        else:
                            self.preallocated_task_count[v] = Capacity(ncpus=1)
                            self.preallocated_task_resource[v] = resource
                        current_xpu_usage.incr_ncpus(1)
                        self.entities_usage[task_entity].add_current_usage(Capacity(ncpus=1))
                    available_xpus = capacity - current_xpu_usage
                    self.avail_resource[resource] = available_xpus
                    self.worker._logger.debug("\tresource %s: - free %s", resource, available_xpus)

                return len(resource_mgr.avail_resource) > 0

        with self._redis.acquire_lock('service:'+service.name):
            queue = 'queued:%s' % service.name
            count = self._redis.llen(queue)
            if count == 0:
                return
            # preallocated_task_count = {}
            # preallocated_task_resource = {}
            # avail_resource = {}
            # resources = service.list_resources()
            # reserved = {}
            # entities_usage = EntityUsage.initialize_entities_usage(self._redis, service.name)
            # # list free cpu/gpus on each node
            # for resource in resources:
            #     current_xpu_usage = Capacity()
            #     capacity = resources[resource]
            #     keygr = 'gpu_resource:%s:%s' % (self._service, resource)
            #     keycr = 'cpu_resource:%s:%s' % (self._service, resource)
            #     key_reserved = 'reserved:%s:%s' % (service.name, resource)
            #
            #     gpu_tasks = self._redis.hgetall(keygr)
            #     cpu_tasks = self._redis.hgetall(keycr)
            #     task_reserved = self._redis.get(key_reserved)
            #
            #     # can not launch multiple tasks on service with no multi-tasking (ec2)
            #     if not service.resource_multitask and not task_reserved and (gpu_tasks or cpu_tasks):
            #         continue
            #
            #     for k, v in six.iteritems(gpu_tasks):
            #         task_entity = task.get_owner_entity(self._redis, v)
            #         if v in preallocated_task_count:
            #             preallocated_task_count[v].incr_ngpus(1)
            #         else:
            #             preallocated_task_count[v] = Capacity(ngpus=1)
            #             preallocated_task_resource[v] = resource
            #         current_xpu_usage.incr_ngpus(1)
            #         entities_usage[task_entity].add_current_usage(Capacity(ngpus=1))
            #
            #     for k, v in six.iteritems(cpu_tasks):
            #         task_entity = task.get_owner_entity(self._redis, v)
            #         if v in preallocated_task_count:
            #             preallocated_task_count[v].incr_ncpus(1)
            #         else:
            #             preallocated_task_count[v] = Capacity(ncpus=1)
            #             preallocated_task_resource[v] = resource
            #         current_xpu_usage.incr_ncpus(1)
            #         entities_usage[task_entity].add_current_usage(Capacity(ncpus=1))
            #     available_xpus = capacity - current_xpu_usage
            #     avail_resource[resource] = available_xpus
            #     reserved[resource] = task_reserved
            #     self._logger.debug("\tresource %s - reserved: %s - free %s",
            #                        resource, task_reserved or "False",
            #                        available_xpus)
            resource_mgr = ResourceManager(self)
            if not resource_mgr.exists_available_resource(service.name):
                return
            # Go through the tasks, find if there are tasks that can be launched and
            # queue the best one
            best_task = None
            task_by_entities = {}
            while count > 0:
                count -= 1
                next_task_id = self._redis.lindex(queue, count)
                candidate_task = CandidateTask.try_create(next_task_id)
                if candidate_task and candidate_task.is_higher_priority(best_task):
                    best_task = candidate_task

            if best_task:
                self._logger.info('selected %s to be launched on %s', best_task._task_id, service.name)
                task.work_queue(self._redis, best_task._task_id, service.name)
                self._redis.lrem(queue, 0, best_task._task_id)

    def _get_current_config(self, task_id):
        task_entity = task.get_owner_entity(self._redis, task_id)
        storages_entities_filter = task.get_storages_entity(self._redis, task_id)
        current_config = config.get_entity_cfg_from_redis(self._redis, self._service, storages_entities_filter, task_entity)
        return current_config