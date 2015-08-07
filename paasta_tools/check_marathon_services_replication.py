#!/usr/bin/env python
"""
Usage: ./check_marathon_services_replication.py [options]

This is a script that checks the number of HAProxy backends via Synapse against
the expected amount that should've been deployed via Marathon in a mesos cluster.

Basically, the script checks smartstack.yaml for listed namespaces, and then queries
Synapse for the number of available backends for that namespace. It then goes through
the Marathon service configuration file for that cluster, and sees how many instances
are expected to be available for that namespace based on the number of instances deployed
on that namespace.

After retrieving that information, a fraction of available instances is calculated
(available/expected), and then compared against a threshold. The default threshold
is .50, meaning if fewer than 50 of a service's backends are available, the script
sends CRITICAL.
"""

import argparse
import logging
import pysensu_yelp
import service_configuration_lib
import sys

from paasta_tools.monitoring import replication_utils
from paasta_tools.monitoring.context import get_context
from paasta_tools import marathon_tools
from paasta_tools import mesos_tools
from paasta_tools import monitoring_tools
from paasta_tools.utils import _log
from paasta_tools.paasta_serviceinit import get_running_tasks_from_active_frameworks


ID_SPACER = marathon_tools.ID_SPACER
log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler(sys.stdout))


def send_event(service_name, namespace, soa_dir, status, output):
    """Send an event to sensu via pysensu_yelp with the given information.

    :param service_name: The service name the event is about
    :param namespace: The namespace of the service the event is about
    :param soa_dir: The service directory to read monitoring information from
    :param status: The status to emit for this event
    :param output: The output to emit for this event"""
    # This function assumes the input is a string like "mumble.main"
    framework = 'marathon'
    check_name = 'check_marathon_services_replication.%s%s%s' % (service_name, ID_SPACER, namespace)
    team = monitoring_tools.get_team(framework, service_name, soa_dir=soa_dir)
    if not team:
        return
    runbook = monitoring_tools.get_runbook(framework, service_name, soa_dir=soa_dir)
    cluster = marathon_tools.get_cluster()
    result_dict = {
        'tip': monitoring_tools.get_tip(framework, service_name, soa_dir=soa_dir),
        'notification_email': monitoring_tools.get_notification_email(framework, service_name, soa_dir=soa_dir),
        'page': monitoring_tools.get_page(framework, service_name, soa_dir=soa_dir),
        'irc_channels': monitoring_tools.get_irc_channels(framework, service_name, soa_dir=soa_dir),
        'ticket': monitoring_tools.get_ticket(framework, service_name, soa_dir=soa_dir),
        'project': monitoring_tools.get_project(framework, service_name, soa_dir=soa_dir),
        'alert_after': '2m',
        'check_every': '1m',
        'realert_every': -1,
        'source': 'paasta-%s' % cluster
    }
    pysensu_yelp.send_event(check_name, runbook, status, output, team, **result_dict)
    _log(
        service_name=service_name,
        line='Replication: %s' % output,
        component='monitoring',
        level='debug',
        cluster=cluster,
        instance=namespace
    )


def parse_args():
    epilog = "PERCENTAGE is an integer value representing the percentage of available to expected instances"
    parser = argparse.ArgumentParser(epilog=epilog)

    parser.add_argument('-c', '--critical', dest='crit', type=int,
                        metavar='PERCENTAGE', default=50,
                        help="Generate critical state if fraction of instances \
                        available is less than this percentage")
    parser.add_argument('-d', '--soa-dir', dest="soa_dir", metavar="SOA_DIR",
                        default=service_configuration_lib.DEFAULT_SOA_DIR,
                        help="define a different soa config directory")
    parser.add_argument('-v', '--verbose', action='store_true',
                        dest="verbose", default=False)
    options = parser.parse_args()

    return options


def split_id(fid):
    """Split a service_name.namespace id into a tuple of
    (service_name, namespace).

    :param fid: The full id to split
    :returns: A tuple of (service_name, namespace)"""
    return (fid.split(ID_SPACER)[0], fid.split(ID_SPACER)[1])


def check_smartstack_replication_for_instance(
    service,
    instance,
    smartstack_replication_info,
    soa_dir,
    crit_threshold,
    expected_count,
):
    """Check a set of namespaces to see if their number of available backends is too low,
    emitting events to Sensu based on the fraction available and the thresholds given.

    :param service: A string like example_service
    :param namespace: A nerve namespace, like "main"
    :param smartstack_replication_info: a dictionary of the form:
                                        {
                                            'unique_location_name': {
                                                'service_name.instance_name': <# ofavailable backends>
                                            },
                                            'other_unique_location_name': ...
                                        }
    :param soa_dir: The SOA configuration directory to read from
    :param crit_threshold: The fraction of instances that need to be up to avoid a CRITICAL event
    """
    namespace = marathon_tools.read_namespace_for_service_instance(service, instance, soa_dir=soa_dir)
    if namespace != instance:
        log.debug("Instance %s is announced under namespace: %s. "
                  "Not checking replication for it" % (instance, namespace))
        return
    full_name = "%s%s%s" % (service, ID_SPACER, instance)
    log.info('Checking instance %s', full_name)

    if len(smartstack_replication_info) == 0:
        status = pysensu_yelp.Status.CRITICAL
        output = ('Service %s has no Smartstack replication info. Make sure the discover key in your smartstack.yaml '
                  'is valid!\n') % full_name
        output = add_context_to_event(service, instance, output)
        log.error(output)
    else:
        expected_count_per_location = int(expected_count / len(smartstack_replication_info))
        output = ''
        under_replication_per_location = []

        for location, available_backends in sorted(smartstack_replication_info.iteritems()):
            num_available_in_location = available_backends.get(full_name, 0)
            under_replicated, ratio = is_under_replicated(
                num_available_in_location, expected_count_per_location, crit_threshold)
            if under_replicated:
                output += '- Service %s has %d out of %d expected instances in %s (CRITICAL: %d%%)\n' % (
                    full_name, num_available_in_location, expected_count_per_location, location, ratio)
            else:
                output += '- Service %s has %d out of %d expected instances in %s (OK: %d%%)\n' % (
                    full_name, num_available_in_location, expected_count_per_location, location, ratio)
            under_replication_per_location.append(under_replicated)

        if any(under_replication_per_location):
            status = pysensu_yelp.Status.CRITICAL
            output = add_context_to_event(service, instance, output)
            log.error(output)
        else:
            status = pysensu_yelp.Status.OK
            log.info(output)
    send_event(service, instance, soa_dir, status, output)


def add_context_to_event(service, instance, output):
    context = get_context(service, instance)
    output = '%s\n%s' % (output, context)
    return output


def is_under_replicated(num_available, expected_count, crit_threshold):
    if expected_count == 0:
        ratio = 100
    else:
        ratio = (num_available / float(expected_count)) * 100

    if ratio < crit_threshold:
        return (True, ratio)
    else:
        return (False, ratio)


def check_mesos_replication_for_service(service, instance, soa_dir, crit_threshold, expected_count):
    num_available = len(get_running_tasks_from_active_frameworks(service, instance))
    # Non-Smartstack services aren't aware of replication within specific
    # locations (since they don't define an advertise/discover level)
    send_event_if_under_replication(
        service=service,
        instance=instance,
        crit_threshold=crit_threshold,
        expected_count=expected_count,
        num_available=num_available,
        soa_dir=soa_dir,
    )


def send_event_if_under_replication(
    service,
    instance,
    crit_threshold,
    expected_count,
    num_available,
    soa_dir,
):
    full_name = "%s%s%s" % (service, ID_SPACER, instance)
    output = ('Service %s has %d out of %d expected instances available!\n' +
              '(threshold: %d%%)') % (full_name, num_available, expected_count, crit_threshold)
    under_replicated, _ = is_under_replicated(num_available, expected_count, crit_threshold)
    if under_replicated:
        log.error(output)
        status = pysensu_yelp.Status.CRITICAL
    else:
        log.info(output)
        status = pysensu_yelp.Status.OK
    send_event(service, instance, soa_dir, status, output)


def check_service_replication(service, instance, crit_threshold, smartstack_replication_info, soa_dir):
    """Checks a service's replication levels based on how the service's replication
    should be monitored. (smartstack or mesos)

    :param service: Service name, like "example_service"
    :param instance: Instance name, like "main" or "canary"
    :param crit_threshold: an int from 0-100 representing the percentage threshold for triggering an alert
    :param soa_dir: The SOA configuration directory to read from
    :smartstack_replication_info: a dictionary of locations where the key is the name of the location
                                      (e.g. 'ca-datacenter1') and the value is a dictionary that contains
                                      the current replication levels from smartstack of the form
                                      {'service_name.instance_name': <available backend count>}. More info
                                      about locations can be found at
                                      https://trac.yelpcorp.com/wiki/Habitat_Datacenter_Ecosystem_Runtimeenv_Region_Superregion
    """
    try:
        expected_count = marathon_tools.get_expected_instance_count_for_namespace(service, instance, soa_dir=soa_dir)
    except marathon_tools.NoDeploymentsAvailable:
        log.info('deployments.json missing for %s.%s. Skipping replication monitoring.' % (service, instance))
        return
    if expected_count is None:
        return
    log.info("Expecting %d total tasks for %s.%s" % (expected_count, service, instance))
    proxy_port = marathon_tools.get_proxy_port_for_instance(service, instance, soa_dir=soa_dir)
    if proxy_port is not None:
        check_smartstack_replication_for_instance(service, instance, smartstack_replication_info,
                                                  soa_dir, crit_threshold, expected_count)
    else:
        check_mesos_replication_for_service(service, instance, soa_dir, crit_threshold, expected_count)


def load_smartstack_info_for_services(service_instances, namespaces, soa_dir):
    """Retrives number of available backends for given services

    :param service_instances: A list of tuples of (service_name, instance_name)
    :param namespaces: list of Smartstack namespaces
    :returns: a dictionary of the form:
              {
                'location_type': {
                    'unique_location_name': {
                        'service_name.instance_name': <# ofavailable backends>
                    },
                    'other_unique_location_name': ...
                }
              }
    """
    smartstack_replication_info = {}
    location_types = set()
    for service_name, instance_name in service_instances:
        service_namespace_config = marathon_tools.load_service_namespace_config(service_name, instance_name,
                                                                                soa_dir=soa_dir)
        discover_location_type = service_namespace_config.get_discover()
        location_types.add(discover_location_type)

    for location_type in location_types:
        smartstack_replication_info[location_type] = get_smartstack_replication_for_attribute(
            location_type, namespaces)

    return smartstack_replication_info


def get_smartstack_replication_for_attribute(attribute, namespaces):
    """Loads smartstack replication from a host with the specified attribute

    :param attribute: a Mesos attribute
    :param namespaces: list of Smartstack namespaces
    :returns: a dictionary of the form {'<unique_attribute_value>': <smartstack replication hash>}
              (the dictionary will contain keys for unique all attribute values)
  """
    replication_info = {}
    unique_values = mesos_tools.get_mesos_slaves_grouped_by_attribute(attribute)

    for value, hosts in unique_values.iteritems():
        # arbitrarily choose the first host with a given attribute to query for replication stats
        synapse_host = hosts[0]
        repl_info = replication_utils.get_replication_for_services('%s:3212' % synapse_host, namespaces)
        replication_info[value] = repl_info

    return replication_info


def main():
    args = parse_args()
    soa_dir = args.soa_dir
    crit_threshold = args.crit
    logging.basicConfig()
    if args.verbose:
        log.setLevel(logging.INFO)
    else:
        log.setLevel(logging.WARNING)
    service_instances = marathon_tools.get_marathon_services_for_cluster(soa_dir=args.soa_dir)
    all_namespaces = [name for name, config in marathon_tools.get_all_namespaces()]

    smartstack_replication_info = load_smartstack_info_for_services(service_instances, all_namespaces, soa_dir)

    for service, instance in service_instances:
        service_namespace_config = marathon_tools.load_service_namespace_config(service, instance, soa_dir=soa_dir)
        discover_location_type = service_namespace_config.get_discover()

        check_service_replication(
            service,
            instance,
            crit_threshold,
            smartstack_replication_info[discover_location_type],
            soa_dir
        )


if __name__ == "__main__":
    if marathon_tools.is_mesos_leader():
        main()
