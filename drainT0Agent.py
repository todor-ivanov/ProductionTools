#!/usr/bin/env python
"""
Does a bunch of checks in order to evaluate the readiness of
an agent getting retired. Checks like:
* whether workflows are injected or not
* whether there are unfinished subscriptions
* whether there are jobs in condor
* files not uploaded to DBS
* files not injected into PhEDEx
 """
from __future__ import print_function

import argparse
import logging
import os
import sys
import threading
from pprint import pprint, pformat
from time import gmtime, strftime

try:
    import htcondor as condor
    from Utils.IteratorTools import flattenList
    from WMCore.WMInit import connectToDB
    from WMCore.Database.DBFormatter import DBFormatter
    from WMCore.Configuration import loadConfigurationFile
    from WMCore.Services.RequestDB.RequestDBReader import RequestDBReader
    # from WMCore.DAOFactory import DAOFactory
except ImportError as e:
    print("You do not have a proper environment (%s), please source the following:" % str(e))
    print("source /data/tier0/srv/wmagent/current/sw/slc7_amd64_gcc630/cms/wmagent/*/etc/profile.d/init.sh")
    sys.exit(1)

jobCountByState = """
    select wmbs_job_state.name, count(*) AS count
      from wmbs_job
      join wmbs_job_state on (wmbs_job.state = wmbs_job_state.id)
      group by wmbs_job.state, wmbs_job_state.name
    """

incompleteWfs = "SELECT DISTINCT name FROM dbsbuffer_workflow WHERE completed = 0"

knownWorkflows = "SELECT DISTINCT name from wmbs_workflow"

workflowsNotInjected = "select distinct name from wmbs_workflow where injected = 0"

unfinishedSubscriptions = """
   select distinct wmbs_workflow.name AS wfName
     FROM wmbs_subscription
     INNER JOIN wmbs_fileset ON wmbs_subscription.fileset = wmbs_fileset.id
     INNER JOIN wmbs_workflow ON wmbs_workflow.id = wmbs_subscription.workflow
     where wmbs_subscription.finished = 0 ORDER BY wmbs_workflow.name
   """

filesAvailWMBS = "select subscription,count(*) from wmbs_sub_files_available group by subscription"

filesAcqWMBS = "select subscription,count(*) from wmbs_sub_files_acquired group by subscription"

blocksOpenDBS = "SELECT * FROM dbsbuffer_block WHERE status!='Closed'"

filesNotInDBS = "SELECT lfn from dbsbuffer_file where status = 'NOTUPLOADED'"

filesNotInPhedex = """
    SELECT lfn FROM dbsbuffer_file
      WHERE in_phedex=0
      AND block_id IS NOT NULL
      AND lfn NOT LIKE '%%unmerged%%'
      AND lfn NOT LIKE 'MCFakeFile%%'
      AND lfn NOT LIKE '%%BACKFILL%%'
      AND lfn NOT LIKE '/store/backfill/%%'
      AND lfn NOT LIKE '/store/user%%'
    """
filesNotInPhedexNull = """
    SELECT lfn FROM dbsbuffer_file
      WHERE in_phedex=0
      AND block_id IS NULL
      AND lfn NOT LIKE '%%unmerged%%'
      AND lfn NOT LIKE 'MCFakeFile%%'
      AND lfn NOT LIKE '%%BACKFILL%%'
      AND lfn NOT LIKE '/store/backfill/%%'
      AND lfn NOT LIKE '/store/user%%'
    """

workflowsExecuting = """
    SELECT wmbs_workflow.name AS name, count(wmbs_job.id) AS count
    FROM wmbs_job
    INNER JOIN wmbs_jobgroup ON wmbs_job.jobgroup = wmbs_jobgroup.id
    INNER JOIN wmbs_subscription ON wmbs_jobgroup.subscription = wmbs_subscription.id
    INNER JOIN wmbs_sub_types ON wmbs_subscription.subtype = wmbs_sub_types.id
    INNER JOIN wmbs_job_state ON wmbs_job.state = wmbs_job_state.id
    INNER JOIN wmbs_workflow ON wmbs_subscription.workflow = wmbs_workflow.id
    WHERE wmbs_job_state.name = 'executing' GROUP BY wmbs_workflow.name
    """


def printWfStatus(wfs, workflowsDict):
    """
    Given a list of request names, print their RequestStatus from the short dictionary.
    """
    for wf in wfs:
        print("%-125s\t%s" % (wf, workflowsDict[wf]['RequestStatus']))

    return


def lfn2dset(lfns):
    """ Convert a LFN into a dataset name """
    if isinstance(lfns, basestring):
        lfns = [lfns]

    listDsets = set()
    for lfn in lfns:
        toks = lfn.split('/')[3:]
        dset = "/%s/%s-%s/%s" % (toks[1], toks[0], toks[3], toks[2])
        listDsets.add(dset)

    return listDsets


def getDsetAndWf(lfns, wfsDict):
    """
    Given a list of lfns (files not injected in DBS/TMDB), get their dataset
    name and compare against the outputdatasets of workflows known by this agent.

    If there is no match, then the workflow has been archived already and there
    is nothing to worry about.

    If the workflow is known, then we may need to recover these files.
    """
    if not lfns:
        return
    uniqLfns = set([lfn.rsplit('/', 2)[0] for lfn in lfns])
    uniqDsets = lfn2dset(uniqLfns)
    print("==> Which maps to %d unique datasets:\n%s" % (len(uniqDsets), pformat(uniqDsets)))

    match = []
    for dset in uniqDsets:
        for wf, values in wfsDict.iteritems():
            if dset in values['OutputDatasets']:
                match.append((wf, values['RequestStatus'], dset))
    if match:
        print("... that were produced by the following workflow, status and the dataset itself:\n")
        pprint(match)
    else:
        print("... that were NOT produced by any agent-known workflow. OR, the wfs are gone already.\n")

    return


def fetchWorkflowsSpec(config, listOfWfs):
    """
    Fetch the workload of a list of workflows. Filter out only a few
    usefull keys
    """
    if isinstance(listOfWfs, basestring):
        listOfWfs = [listOfWfs]

    wfDBReader = RequestDBReader(config.AnalyticsDataCollector.centralRequestDBURL,
                                 couchapp=config.AnalyticsDataCollector.RequestCouchApp)
    tempWfs = wfDBReader.getRequestByNames(listOfWfs, True)

    wfShortDict = {}
    for wf in listOfWfs:
        tempWfs[wf].pop("RequestTransition", None)
        tempWfs[wf].pop("_id", None)
        tempWfs[wf].pop("RequestName", None)
        wfShortDict[wf] = tempWfs[wf]
    return wfShortDict


def getCondorJobs():
    """
    Retrieve jobs from condor.
    :return: amount of jobs ordered by JobStatus and workflow
    """
    jobDict = {}
    schedd = condor.Schedd()
    jobs = schedd.xquery('WMAgent_AgentName == "WMAgentCommissioning"', ['WMAgent_RequestName', 'JobStatus'])
    for job in jobs:
        jobStatus = job['JobStatus']
        jobDict.setdefault(jobStatus, {})
        jobDict[jobStatus].setdefault(job['WMAgent_RequestName'], 0)
        jobDict[jobStatus][job['WMAgent_RequestName']] += 1
    return jobDict


def getWMBSInfo(config):
    """
    blah
    :return:
    """
    connectToDB()
    myThread = threading.currentThread()
    formatter = DBFormatter(logging, myThread.dbi)

    workflows = formatter.formatDict(myThread.dbi.processData(knownWorkflows))
    workflows = [wf['name'] for wf in workflows]
    print("\n*** WORKFLOWS: found %d distinct workflows in this agent." % len(workflows))
    workflowsDict = fetchWorkflowsSpec(config, workflows)
    printWfStatus(workflows, workflowsDict)

    workflows = formatter.formatDict(myThread.dbi.processData(incompleteWfs))
    workflows = [wf['name'] for wf in workflows]
    print("\n*** WORKFLOWS: there are %d distinct workflows not completed." % len(workflows))
    printWfStatus(workflows, workflowsDict)

    wfsNotInjected = flattenList(formatter.format(myThread.dbi.processData(workflowsNotInjected)))
    print("\n*** WORKFLOWS: found %d workflows not fully injected." % len(wfsNotInjected))
    printWfStatus(wfsNotInjected, workflowsDict)

    jobsByState = formatter.formatDict(myThread.dbi.processData(jobCountByState))
    print("\n*** WMBS: amount of wmbs jobs in each status:\n%s" % jobsByState)
    # IF we have executing jobs in wmbs and nothing in condor, then investigate the wfs
    if 'executing' in [item['name'] for item in jobsByState]:
        wfsJobCount = formatter.formatDict(myThread.dbi.processData(workflowsExecuting))
        print("\n*** WMBS: %d workflows with executing jobs in wmbs:" % len(wfsJobCount))
        workflows = [wf['name'] for wf in wfsJobCount]
        printWfStatus(workflows, workflowsDict)

    unfinishedSubs = formatter.formatDict(myThread.dbi.processData(unfinishedSubscriptions))
    unfinishedSubs = [wf['wfname'] for wf in unfinishedSubs]
    print("\n*** SUBSCRIPTIONS: subscriptions not finished: %d" % len(unfinishedSubs))
    printWfStatus(unfinishedSubs, workflowsDict)

    filesAvailable = formatter.formatDict(myThread.dbi.processData(filesAvailWMBS))
    print(
        "\n*** SUBSCRIPTIONS: found %d files available in WMBS (waiting for job creation):\n%s" % (len(filesAvailable),
                                                                                                   filesAvailable))

    filesAcquired = formatter.formatDict(myThread.dbi.processData(filesAcqWMBS))
    print(
        "\n*** SUBSCRIPTIONS: found %d files acquired in WMBS (waiting for jobs to finish):\n%s" % (len(filesAcquired),
                                                                                                    filesAcquired))

    blocksopenDBS = formatter.formatDict(myThread.dbi.processData(blocksOpenDBS))
    print("\n*** DBS: found %d blocks open in DBS." % len(blocksopenDBS), end="")
    print(" Printing the first 20 blocks only:\n%s" % blocksopenDBS[:20])

    filesnotinDBS = flattenList(formatter.format(myThread.dbi.processData(filesNotInDBS)))
    print("\n*** DBS: found %d files not uploaded to DBS.\n" % len(filesnotinDBS))
    getDsetAndWf(filesnotinDBS, workflowsDict)

    filesnotinPhedex = flattenList(formatter.format(myThread.dbi.processData(filesNotInPhedex)))
    print("\n*** PHEDEX: found %d files not injected in PhEDEx, with valid block id (recoverable)." % len(
        filesnotinPhedex))
    getDsetAndWf(filesnotinPhedex, workflowsDict)

    filesnotinPhedexNull = flattenList(formatter.format(myThread.dbi.processData(filesNotInPhedexNull)))
    print("\n*** PHEDEX: found %d files not injected in PhEDEx, with valid block id (unrecoverable)." % len(
        filesnotinPhedexNull))
    getDsetAndWf(filesnotinPhedexNull, workflowsDict)


def parseArgs():
    """
    Well, parse the arguments passed in the command line :)
    """
    parser = argparse.ArgumentParser(description="Does a bunch of draining checks")
    parser.add_argument('-t', '--twiki', action='store_true', default=False,
                        help='Use it to get an output that can be directly pasted in a twiki')
    args = parser.parse_args()
    return args


def main():
    """
    Retrieve the following information from the agent:
      1. number of jobs in condor
      2. list of distinct workflows in wmbs_workflow (and their status in reqmgr2)
      3. amount of wmbs jobs in each status
      4. list of workflows not fully injected
      5. list of subscriptions not finished
      6. amount of files available in wmbs
      7. amount of files acquired in wmbs
      8. list of blocks not closed in phedex/dbs
      9. list of files not uploaded to dbs
      10. list of files not injected into phedex, with parent block
      11. list of files not injected into phedex, without parent block
    """
    args = parseArgs()

    twiki = ('<pre>', '</pre>') if args.twiki else ('', '')
    print(twiki[0])

    os.environ['WMAGENT_CONFIG'] = '/data/tier0/srv/wmagent/current/config/tier0/config.py'
    config = loadConfigurationFile(os.environ["WMAGENT_CONFIG"])

    gmtTimeNow = strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime())
    print("Drain check time: %s\n" % gmtTimeNow)

    print("*** Amount of jobs in condor per workflow, sorted by condor job status:")
    pprint(getCondorJobs())

    getWMBSInfo(config)

    print(twiki[1])
    print("I'm done!")
    sys.exit(0)


if __name__ == '__main__':
    sys.exit(main())
