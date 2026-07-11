import socket
import json
import logging
import time
from ipaddress import IPv4Address

log = logging.getLogger(__name__)


'''
cmd: the command you want the miner to run in the format '{'command': 'devdetails'}'
ip: the ip address of a miner e.g. '192.168.1.1' as a string
port: the port the miner is listening to API calls on. By default 4028 for Luxor

returns: json python dictionary
'''

def send_cmd(cmd, ip, port=4028):
    #Open raw tcp connection
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, port))

        sock.send(json.dumps(cmd).encode('utf-8'))

        response = sock.recv(4096)

        response = json.loads(response.decode('utf-8'))

        sock.close()

        return response

    except Exception as e:
        log.info("send_cmd failed: " + str(e))

'''
Given the ip address of a miner e.g. '192.168.1.1' as a string

Return True/False based on whether the miner responds to tcp commands
'''
def isAlive(ip):
    # Initial connection
    try:
        vercmd = {'command': 'version'}
        ver = send_cmd(vercmd, ip)

        # Check that connection is successful
        if ver['STATUS'][0]['STATUS'] == "S":
            print('Active Miner at: ' + ip)
            return True
        else:
            print("Connection failed")
            raise Exception("Device did not respond to tcp messages")
    except Exception as e:
        log.info("ip address: " + ip + " is not alive with error " + str(e))
        return False


'''
Takes in a tcp_connect result object as input

Returns true/false based on if the command was run successfully or not
'''
def commandSuccessful(result):
    try:
        if result['STATUS'][0]['STATUS'] == "S":
            # command was successful
            return True
        elif result['STATUS'][0]['STATUS'] == "E":
            # command was unsuccessful, print error message
            print("Command was not run successfully. Error: " + result['STATUS'][0]['Msg'])
    except Exception as e:
        # tcp response malformed
        # log.info("tcp result could not be checked for success/error. Error: " + str(e))
        return False

'''
Takes in a tcp_connect result object as input

Returns true/false based on if the command was run successfully or not
'''
def chipSetSuccessful(result, targetfreq, targetChip):
    try:
        if result['STATUS'][0]['STATUS'] == "S":
            # command was successful
            if result['RAMP'][0]['Frequency'] == str(targetfreq) and result['RAMP'][0]['TargetChip'] == str(targetChip):
                return True
            return False
        elif result['STATUS'][0]['STATUS'] == "E":
            # command was unsuccessful, print error message
            print("Command was not run successfully. Error: " + result['STATUS'][0]['Msg'])
    except Exception as e:
        # tcp response malformed
        # log.info("tcp result could not be checked for success/error. Error: " + str(e))
        return False

def ipRange(start_ip, end_ip):
    """Return the inclusive IPv4 range from *start_ip* through *end_ip*.

    This implementation uses Python's standard-library address arithmetic and
    replaces the unlicensed helper copied into the original research snapshot.
    """
    start = IPv4Address(start_ip)
    end = IPv4Address(end_ip)
    if end < start:
        raise ValueError("end_ip must not be lower than start_ip")
    return [str(IPv4Address(value)) for value in range(int(start), int(end) + 1)]


'''
Given the ip address of a miner e.g. '192.168.1.1' as a string

Opens a session and returns the session ID
'''
def openSession(ip):
    # Check if an existing session already exists
    checkSessionCMD = {"command": "session"}
    checkSessionResult = send_cmd(checkSessionCMD, ip)

    if commandSuccessful(checkSessionResult):
        if checkSessionResult['SESSION'][0]['SessionID'] == "":
            # No Session exists
            openSessionCMD = {"command": "logon"}
            openSessionResult = send_cmd(openSessionCMD, ip)

            if commandSuccessful(openSessionResult):
                return openSessionResult['SESSION'][0]['SessionID']
            else:
                log.error("Open session command failed to succeed")
            return ''
        else:
            #TODO: don't session steal!
            return checkSessionResult['SESSION'][0]['SessionID']
    else:
        log.error("Could not check for existing session")




'''
Given 
ip: the ip address of a miner e.g. '192.168.1.1' as a string
sessionID: the ID of the session you are trying to close formatted as a string

Closes a session and returns True if the command succeeds
'''
def closeSession(ip, sessionID):
    closeSessionCMD = {"command": "logoff", "parameter": sessionID}
    closeSessionResult = send_cmd(closeSessionCMD, ip)

    if commandSuccessful(closeSessionResult):
        return True
    else:
        log.error("close session command failed to succeed")
    return False
