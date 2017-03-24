#!/usr/bin/python
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import argparse
import re
import time

from cm_api.api_client import ApiResource

def parse_args():
    parser = argparse.ArgumentParser(description="Uses Cloudera Manager to upgrade the KUDU "
                                                 "parcel to the newest compatible version. Will "
                                                 "not upgrade to a new version of Kudu, i.e. the "
                                                 "release version will be the same on the new "
                                                 "parcel. After this script is successfully run, "
                                                 "the old parcel will no longer be considered "
                                                 "activated, but existing KUDU services must be "
                                                 "restarted to use the new parcel.")
    parser.add_argument("--host", type=str, default="localhost",
                        help="Hostname of the Cloudera Manager server. Default is localhost.")
    parser.add_argument("--user", type=str, default="admin",
                        help="Username with which to log into Cloudera Manager. Default is 'admin'.")
    parser.add_argument("--password", type=str, default="admin",
                        help="Password with which to log into Cloudera Manager. Default is 'admin'.")
    parser.add_argument("--cluster", type=str,
                        help="Name of an existing cluster on which the Kudu service should be "
                        "upgraded. If not specified, uses the only cluster available or raises an "
                        "exception if multiple or no clusters are found.")
    parser.add_argument("--max_time_per_stage", type=int, default=120,
                        help="Maximum amount of time in seconds allotted to waiting for any single "
                        "stage of parcel distribution (i.e. downloading, distributing, "
                        "activating). Default is two minutes.")
    return parser.parse_args()

def get_best_upgrade_candidate_parcel(cluster):
    # A parcel is an upgrade candidate if 1) it has the same release version as the currently active
    # parcel, and 2) it is lexicographically greater than the currently active parcel, indicating a
    # newer patch build. The best candidate will be the greatest lexicographically.
    activated_parcels = []
    candidate_parcels = []
    for parcel in cluster.get_all_parcels():
        if parcel.product == "KUDU":
            if parcel.stage == "ACTIVATED":
                activated_parcels.append(parcel)
            else:
                candidate_parcels.append(parcel)

    def release_versions_match(parcel1, parcel2):
        def get_release_version(full_version):
            # The following regexp matches the major, minor, and patch version numbers at the
            # beginning of the full parcel version string.
            # E.g. "1.4.0-1.cdh5.12.0.p0.814" will return "1.4.0"
            match = re.match("(\d+\.\d+\.\d+).*", full_version)
            if match is None:
                raise Exception("Could not get the release version from %s." % full_version)
            return match.group(1)
        return get_release_version(parcel1.version) == get_release_version(parcel2.version)

    if len(activated_parcels) > 0:
        greatest_activated = max(activated_parcels, key=lambda p: p.version)

        # Filter out parcels that have different release versions or are downgrades.
        candidate_parcels = [parcel for parcel in candidate_parcels
                             if release_versions_match(parcel, greatest_activated) and
                                parcel.version > greatest_activated.version]
        if len(candidate_parcels) > 0:
            greatest_candidate = max(candidate_parcels, key=lambda p: p.version)
            print("Chose the new parcel %s-%s (Stage: %s)." % (greatest_candidate.product,
                                                               greatest_candidate.version,
                                                               greatest_candidate.stage))
            return greatest_candidate
        else:
            raise Exception("No upgrade candidates available for parcel version %s-%s." %
                            (greatest_activated.product, greatest_activated.version))
    raise Exception("No activated KUDU parcels found. Activate one first and then upgrade.")

def wait_for_parcel_stage(cluster, parcel, stage, max_time):
    for attempt in xrange(1, max_time + 1):
        new_parcel = cluster.get_parcel(parcel.product, parcel.version)
        if new_parcel.stage == stage:
            return
        if new_parcel.state.errors:
            raise Exception("Fetching parcel resulted in error %s" % str(new_parcel.state.errors))
        print("progress: %s / %s" % (new_parcel.state.progress, new_parcel.state.totalProgress))
        time.sleep(1)
    else:
        raise Exception("Parcel %s-%s did not reach stage %s in %d seconds." %
                        (parcel.product, parcel.version, stage, max_time))

def ensure_parcel_activated(cluster, parcel, max_time_per_stage):
    parcel_stage = parcel.stage
    if parcel_stage == "AVAILABLE_REMOTELY":
        print("Downloading parcel: %s-%s" % (parcel.product, parcel.version))
        parcel.start_download()
        wait_for_parcel_stage(cluster, parcel, "DOWNLOADED", max_time_per_stage)
        print("Downloaded parcel: %s-%s " % (parcel.product, parcel.version))
        parcel_stage = "DOWNLOADED"
    if parcel_stage == "DOWNLOADED":
        print("Distributing parcel: %s-%s " % (parcel.product, parcel.version))
        parcel.start_distribution()
        wait_for_parcel_stage(cluster, parcel, "DISTRIBUTED", max_time_per_stage)
        print("Distributed parcel: %s-%s " % (parcel.product, parcel.version))
        parcel_stage = "DISTRIBUTED"
    if parcel_stage == "DISTRIBUTED":
        print("Activating parcel: %s-%s " % (parcel.product, parcel.version))
        parcel.activate()
        wait_for_parcel_stage(cluster, parcel, "ACTIVATED", max_time_per_stage)
        print("Activated parcel: %s-%s " % (parcel.product, parcel.version))

def find_cluster(api, cluster_name):
    if cluster_name:
        return api.get_cluster(cluster_name)
    all_clusters = api.get_all_clusters()
    if len(all_clusters) == 0:
        raise Exception("No clusters found; create one before calling this script.")
    if len(all_clusters) > 1:
        raise Exception("More than one cluster found; specify which cluster to use using --cluster.")
    cluster = all_clusters[0]
    print("Found cluster: %s" % cluster.displayName)
    return cluster

def main():
    args = parse_args()
    api = ApiResource(args.host,
                      username=args.user,
                      password=args.password,
                      version=10)
    cluster = find_cluster(api, args.cluster)

    # Get the parcels available to this cluster. Get the newest one that is not activated, ensuring
    # that it is lexicographically greater than that ACTIVATED and that it distributes the same
    # release version of Kudu.
    parcel = get_best_upgrade_candidate_parcel(cluster)

    # Start up the upgrade process and activate the new parcel.
    ensure_parcel_activated(cluster, parcel, args.max_time_per_stage)

if __name__ == "__main__":
    main()