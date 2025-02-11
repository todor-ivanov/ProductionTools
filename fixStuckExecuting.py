#!/usr/bin/env python
"""
Script used to update an inconsistency problem found in production at July 7, 2016
The problem was that there were much more jobs as 'executing' in wmbs_job than
what actually was in condor (or in the BossAir table, bl_runjob).

The idea is that we mark those jobs 'executing' for more than 6 days as 'jobfailed',
if the workflow is still running. Otherwise we mark jobs as 'cleanout'
"""
import sys
import os
import json
import threading
import logging
import time
import urllib2
import urllib
import httplib
import htcondor as condor
from pprint import pprint
from WMCore.WMInit import connectToDB
from WMCore.Database.DBFormatter import DBFormatter


getJobsExecuting = """
                   SELECT wmbs_workflow.name, count(wmbs_job.id) AS count FROM wmbs_job
                     INNER JOIN wmbs_jobgroup ON wmbs_job.jobgroup = wmbs_jobgroup.id
                     INNER JOIN wmbs_subscription ON wmbs_jobgroup.subscription = wmbs_subscription.id
                     INNER JOIN wmbs_workflow ON wmbs_subscription.workflow = wmbs_workflow.id
                     WHERE wmbs_job.state=(SELECT id from wmbs_job_state where name='executing') 
                     AND wmbs_job.state_time < :timestamp GROUP BY wmbs_workflow.name
                   """

getWMBSIds = """
             SELECT wmbs_job.id FROM wmbs_job
             INNER JOIN wmbs_jobgroup ON wmbs_job.jobgroup = wmbs_jobgroup.id
             INNER JOIN wmbs_subscription ON wmbs_jobgroup.subscription = wmbs_subscription.id
             INNER JOIN wmbs_sub_types ON wmbs_subscription.subtype = wmbs_sub_types.id
             INNER JOIN wmbs_job_state ON wmbs_job.state = wmbs_job_state.id
             INNER JOIN wmbs_workflow ON wmbs_subscription.workflow = wmbs_workflow.id
             WHERE wmbs_workflow.name = :wfname AND wmbs_job.state_time < :timestamp AND wmbs_job_state.name = 'executing'
             """

updateState = """
              UPDATE wmbs_job SET state=(SELECT id from wmbs_job_state WHERE name = :state) WHERE id = :id
              """


CACHE_STATUS = {'fabozzi_HIRun2015-HIMinimumBias5-02May2016_758p4_160502_172625_4322': u'running-closed',
 'pdmvserv_task_HIG-RunIISpring16DR80-01259__v1_T_160620_125903_751': u'running-closed',
 'prozober_task_B2G-RunIISpring16DR80-00683__v1_T_160616_114831_7202': u'running-closed',
 'vlimant_task_BPH-RunIIFall15DR76-00036__v1_T_160609_150828_2504': u'running-closed',
 'vlimant_task_BPH-RunIIFall15DR76-00044__v1_T_160609_151017_3729': u'aborted-archived',
 'vlimant_task_SUS-RunIIFall15DR76-00109__v1_T_160609_150914_3262': u'running-closed',
 'vlimant_task_SUS-RunIIFall15DR76-00120__v1_T_160609_143246_6395': u'running-closed'}


def getStatus(workflow):
    # for some reason I'm getting a damn SSL error in submit2... 
    # ssl.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:591)
    global CACHE_STATUS
    if workflow in CACHE_STATUS:
        return CACHE_STATUS.get(workflow)
    url = 'cmsweb.cern.ch'
    headers = {"Accept": "application/json", "Content-type": "application/json"}
    conn = httplib.HTTPSConnection(url, cert_file = os.getenv('X509_USER_PROXY'), key_file = os.getenv('X509_USER_PROXY'))
    urn = "/reqmgr2/data/request/%s" % workflow
    conn.request("GET", urn, headers=headers)
    r2=conn.getresponse()
    try:
        request = json.loads(r2.read())["result"][0]
        status = request[workflow]['RequestStatus']
    except (KeyError, ValueError):
        status = 'UNKNOWN'
    return status


def main():
    if 'WMAGENT_CONFIG' not in os.environ:
        os.environ['WMAGENT_CONFIG'] = '/data/srv/wmagent/current/config/wmagent/config.py'
    if 'manage' not in os.environ:
        os.environ['manage'] = '/data/srv/wmagent/current/config/wmagent/manage'

    timenow = int(time.time())
    time6d = timenow - 6 * 24 * 3600

    connectToDB()
    myThread = threading.currentThread()
    formatter = DBFormatter(logging, myThread.dbi)

    # Get list of workflows and number of jobs executing for more than 6 days
    binds = [{'timestamp': time6d}]
    wmbsJobsPerWf = formatter.formatDict(myThread.dbi.processData(getJobsExecuting, binds))
    totalJobs = sum([int(item['count']) for item in wmbsJobsPerWf])
    print "Found %d workflows with a total of %d jobs" % (len(wmbsJobsPerWf), totalJobs)
    #pprint(wmbsJobsPerWf)

    # Retrieve all jobs from condor schedd
    # it returns an iterator, so let's make it a list such that we can iterate over
    # it several times... why did I notice it only know?!?!
    schedd = condor.Schedd()
    jobs = list(schedd.xquery('true', ['ClusterID', 'ProcId', 'WMAgent_RequestName', 'JobStatus', 'WMAgent_JobID']))

    # Retrieve their status from reqmgr2 and
    # add their wmbsId to the dict
    for item in wmbsJobsPerWf:
        item['status'] = getStatus(item['name'])
        item['condorjobs'] = []
        for job in jobs:
            if job['WMAgent_RequestName'] == item['name']:
                item['condorjobs'].append(job['WMAgent_JobID'])

    #pprint(wmbsJobsPerWf)

    # time to have some ACTION
    for item in wmbsJobsPerWf:
        binds = [{'timestamp': time6d, 'wfname': item['name']}]
        jobIds  = formatter.formatDict(myThread.dbi.processData(getWMBSIds, binds))
        wmbsIds = [x['id'] for x in jobIds]
        print "%-100s in %s. Has %d wmbs and %d condor jobs" % (item['name'], item['status'], len(wmbsIds), len(item['condorjobs']))
        # continue
        # Just skip it if there are condor jobs out there
        if len(item['condorjobs']) > 0 or item['status'] == 'UNKNOWN':
            continue
        newstatus = 'jobfailed' if item['status'] in ('acquired', 'running-open', 'running-closed') else 'cleanout'
        var = raw_input("Marking jobs from %s to %s: (Y/N) " % (item['status'], newstatus))
        if var in ['Y', 'y']:
            print "UPDATED %s" % item['name']
            binds = []
            for x in jobIds:
                x['state'] = newstatus
                binds.append(x)
            myThread.dbi.processData(updateState, binds)

    print "Done!"
    sys.exit(0)


if __name__ == '__main__':
    sys.exit(main())
