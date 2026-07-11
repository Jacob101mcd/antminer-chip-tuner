import tcp_connect
import commands
import logging
from joblib import Parallel, delayed
import config

log = logging.getLogger(__name__)


def getIPs():
    # start = input("Enter Start IP of range: ")
    start = config.start
    # end = input("Enter end IP or range: ")
    end = config.end

    ip_range = tcp_connect.ipRange(start, end)

    compatibleMiners = []
    numbCompatibleMiners = 0

    parallel = Parallel(n_jobs=250, return_as="generator")
    output_generator = list(parallel(delayed(commands.checkCompatibility)(addr) for addr in ip_range))

    for ip in enumerate(ip_range):
        if output_generator[ip[0]]:
            compatibleMiners.append(ip[1])
            numbCompatibleMiners += 1

    print("There are " + str(numbCompatibleMiners) + " compatible miners on the network")
    if numbCompatibleMiners < 1:
        exit(-1)
    else:
        return compatibleMiners

def getAliveIPs():
    # start = input("Enter Start IP of range: ")
    start = config.start
    # end = input("Enter end IP or range: ")
    end = config.end

    ip_range = tcp_connect.ipRange(start, end)

    compatibleMiners = []
    numbCompatibleMiners = 0

    parallel = Parallel(n_jobs=-1, return_as="generator")
    output_generator = list(parallel(delayed(tcp_connect.isAlive)(addr) for addr in ip_range))

    for ip in enumerate(ip_range):
        if output_generator[ip[0]]:
            compatibleMiners.append(ip[1])
            numbCompatibleMiners += 1

    print("There are " + str(numbCompatibleMiners) + " compatible miners on the network")
    if numbCompatibleMiners < 1:
        exit(-1)
    else:
        return compatibleMiners