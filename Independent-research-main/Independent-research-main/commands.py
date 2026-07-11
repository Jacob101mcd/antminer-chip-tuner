import time

import tcp_connect
import config
import logging

log = logging.getLogger(__name__)
RETRIES = 10

'''
Given the ip address of a miner e.g. '192.168.1.1' as a string

Return True/False based on whether the miner responds to pings and has supported software and hardware
'''


def checkCompatibility(ip):
    # Initial connection
    if not tcp_connect.isAlive(ip):
        # ip address is not valid, or connection can't be made
        return False
    else:
        try:
            versionCMD = {'command': 'version'}
            devdetailsCMD = {'command': 'devdetails'}

            ver = tcp_connect.send_cmd(versionCMD, ip)
            dev = tcp_connect.send_cmd(devdetailsCMD, ip)

            # Check that API has reported successful running of command
            if tcp_connect.commandSuccessful(ver) and tcp_connect.commandSuccessful(dev):
                # Check that miner is supported model
                if ver['VERSION'][0]['Type'] in config.supportedModels and ver['VERSION'][0]['LUXminer'] in config.supportedVersions:
                    log.info('Model and Version Supported')
                else:
                    log.info('Model or Version Not Supported!\n' + "Type: " + ver['VERSION'][0]['Type'] + "\nVersion: " +
                          ver['VERSION'][0]['LUXminer'])
                    return False

                # Check that boards are supported
                for board in dev['DEVDETAILS']:
                    if board['Board'] not in config.supportedBoards:
                        f = open(ip + "-log.txt", "a")
                        f.write("Board " + board['Board'] + " not supported!!!\n")
                        f.close()
                        return False
            else:
                # API reported failure to run command
                return False
            # All checks passed
            return True

        except Exception as e:
            log.error("check compatibility resulted in unknown error: " + str(e))
            return False


'''
Given the ip address of a miner e.g. '192.168.1.1' as a string

Return an array of the format [min voltage, default voltage, max voltage, minimum voltage step, min frequency, default frequency, max frequency, min frequency step]
'''


def getLimits(ip):
    results = []

    # Try to get limits info
    try:
        limitsCMD = {"command": "limits"}
        limits = tcp_connect.send_cmd(limitsCMD, ip)

        while not tcp_connect.commandSuccessful(limits):
            # TODO: set limit on number of retries
            limits = tcp_connect.send_cmd(limitsCMD, ip)

        results.append(limits['LIMITS'][0]['VoltageMin'])
        results.append(limits['LIMITS'][0]['VoltageDefault'])
        results.append(limits['LIMITS'][0]['VoltageMax'])
        results.append(limits['LIMITS'][0]['VoltageStepMin'])
        results.append(limits['LIMITS'][0]['FrequencyMin'])
        results.append(limits['LIMITS'][0]['FrequencyDefault'])
        results.append(limits['LIMITS'][0]['FrequencyMax'])
        results.append(limits['LIMITS'][0]['FrequencyStepMin'])
        return results

    except Exception as e:
        log.error("getInfo resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string
voltage: float value that must be between the min and max voltage
board: must be passed in as 0

Returns True/False when successful
'''


def setVoltage(ip, voltage, board=0):
    # Try to set voltage
    try:
        if board > 2:
            return False

        # open session
        sessionID = tcp_connect.openSession(ip)

        parameter = sessionID + ',' + str(board) + ',' + str(voltage)
        setVoltageCMD = {"command": "voltageset", "parameter": parameter}
        setVoltageResult = tcp_connect.send_cmd(setVoltageCMD, ip)

        # close session
        tcp_connect.closeSession(ip, sessionID)

        if tcp_connect.commandSuccessful(setVoltageResult):
            return True
        else:
            return setVoltage(ip, voltage, board + 1)

    except Exception as e:
        log.error("setVoltage resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: int to determine which board to apply to (won't matter for most models)

Returns the voltage of the miner
'''


def getVoltage(ip, board=0):
    # Try to get voltage
    try:
        if board > 2:
            raise ValueError('Could not check voltage')

        getVoltageCMD = {"command": "voltageget", "parameter": board}
        getVoltageResult = tcp_connect.send_cmd(getVoltageCMD, ip)

        if tcp_connect.commandSuccessful(getVoltageResult):
            return getVoltageResult['VOLTAGE'][0]['Voltage']
        else:
            return getVoltage(ip, board + 1)

    except Exception as e:
        log.error("setVoltage resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string
frequency: int value that must be between the min and max frequency and a valid step

Returns True/False when successful
'''


def setBoardFreq(ip, frequency, board):
    # Try to set frequency
    try:
        #open session
        sessionID = tcp_connect.openSession(ip)

        parameter = sessionID + ',' + str(board) + ',' + str(frequency)
        setFreqCMD = {"command": "frequencyset", "parameter": parameter}
        setFreqResult = tcp_connect.send_cmd(setFreqCMD, ip)

        # close session
        tcp_connect.closeSession(ip, sessionID)

        if tcp_connect.commandSuccessful(setFreqResult):
            return True
        else:
            return False

    except Exception as e:
        log.error("setFrequency resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string

Returns the average frequency for the board as an int
'''


def getAverageBoardFreq(ip, board=0):
    # Try to get freq
    try:
        getFreqCMD = {"command": "frequencyget", "parameter": board}
        getFreqResult = tcp_connect.send_cmd(getFreqCMD, ip)

        if tcp_connect.commandSuccessful(getFreqResult):
            freqArray = getFreqResult['FREQS'][0]['Freqs']
            return int(sum(freqArray) / len(freqArray))
        else:
            raise Exception("Failed to get frequency")

    except Exception as e:
        log.error("setFrequency resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string

Returns the average frequency for the board as an int
'''


def getAverageMinerFreq(ip):
    # Try to get freq
    try:
        board = getAverageBoardFreq(ip)
        board1 = getAverageBoardFreq(ip, 1)
        board2 = getAverageBoardFreq(ip, 2)

        return (board + board1 + board2) / 3

    except Exception as e:
        log.error("getAverageMinerFreq resulted in unknown error: " + str(e))
        return False


'''
Checks if the chips have settled (no unknown health status)

ip: the ip address of a miner e.g. '192.168.1.1' as a string

Returns True if known, False if some chips are still unknown

'''


def chipsHealthKnown(ip):
    try:
        #get number of chips on board 0
        getNumbChips = {"command": "frequencyget", "parameter": "0"}
        getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)
        while not tcp_connect.commandSuccessful(getNumbChipsResult):
            getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)

        board0NumbChips = int(getNumbChipsResult['FREQS'][0]['Count'])

        #Get board 0 chip data
        for x in range(board0NumbChips):
            parameter = "0," + str(x)
            cmd = {"command": "healthchipget", "parameter": parameter}
            cmdResult = tcp_connect.send_cmd(cmd, ip)
            while not tcp_connect.commandSuccessful(cmdResult):
                # TODO: Set reasonable limit on retrys
                cmdResult = tcp_connect.send_cmd(cmd, ip)
            if cmdResult['CHIPS'][0]['Healthy'] != 'Y' and cmdResult['CHIPS'][0]['Healthy'] != 'N':
                return False

        # Get board 1 chip data
        # TODO: change to allow for different chips per board
        for x in range(board0NumbChips):
            parameter = "1," + str(x)
            cmd = {"command": "healthchipget", "parameter": parameter}
            cmdResult = tcp_connect.send_cmd(cmd, ip)
            while not tcp_connect.commandSuccessful(cmdResult):
                # TODO: Set reasonable limit on retrys
                cmdResult = tcp_connect.send_cmd(cmd, ip)
            if cmdResult['CHIPS'][0]['Healthy'] != 'Y' and cmdResult['CHIPS'][0]['Healthy'] != 'N':
                return False

        # Get board 2 chip data
        # TODO: change to allow for different chips per board
        for x in range(board0NumbChips):
            parameter = "2," + str(x)
            cmd = {"command": "healthchipget", "parameter": parameter}
            cmdResult = tcp_connect.send_cmd(cmd, ip)
            while not tcp_connect.commandSuccessful(cmdResult):
                # TODO: Set reasonable limit on retrys
                cmdResult = tcp_connect.send_cmd(cmd, ip)
            if cmdResult['CHIPS'][0]['Healthy'] != 'Y' and cmdResult['CHIPS'][0]['Healthy'] != 'N':
                return False

        # All chips are settled, return true
        return True

    except Exception as e:
        log.error("chipHealthKnown resulted in unknown error: " + str(e))
        return False


'''
Retrieves chip health info and returns it as an array

ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: which board to check chips of 

Returns an array of all chips for a given board

'''


def getChipsHealth(ip, board=0):
    try:
        # get number of chips on board
        getNumbChips = {"command": "frequencyget", "parameter": str(board)}
        getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)
        while not tcp_connect.commandSuccessful(getNumbChipsResult):
            getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)

        board0NumbChips = int(getNumbChipsResult['FREQS'][0]['Count'])

        healthyArray = []

        #Get board chip data
        for x in range(board0NumbChips):
            parameter = str(board) + "," + str(x)
            cmd = {"command": "healthchipget", "parameter": parameter}
            cmdResult = tcp_connect.send_cmd(cmd, ip)
            while not tcp_connect.commandSuccessful(cmdResult):
                # TODO: Set reasonable limit on retrys
                cmdResult = tcp_connect.send_cmd(cmd, ip)
            healthyArray.append(cmdResult['CHIPS'][0]['Healthy'])

        return healthyArray

    except Exception as e:
        log.error("getChipsHealth resulted in unknown error: " + str(e))
        return False


'''
Retrieves chip health score (0-100) and returns it as an array

ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: which board to check chips of 

Returns an array of all chips for a given board

'''


def getChipsScore(ip, board=0):
    try:
        # get number of chips on board
        getNumbChips = {"command": "frequencyget", "parameter": str(board)}
        getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)
        while not tcp_connect.commandSuccessful(getNumbChipsResult):
            getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)

        board0NumbChips = int(getNumbChipsResult['FREQS'][0]['Count'])

        healthyArray = []

        #Get board chip data
        for x in range(board0NumbChips):
            parameter = str(board) + "," + str(x)
            cmd = {"command": "healthchipget", "parameter": parameter}
            cmdResult = tcp_connect.send_cmd(cmd, ip)
            while not tcp_connect.commandSuccessful(cmdResult):
                # TODO: Set reasonable limit on retrys
                cmdResult = tcp_connect.send_cmd(cmd, ip)
            healthyArray.append(cmdResult['CHIPS'][0]['Score'])

        return healthyArray


    except Exception as e:
        log.error("getChipsScore resulted in unknown error: " + str(e))
        return False


'''
Retrieves chip freq info and returns it as an array

ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: which board to check chips of 

Returns an array of all chips for a given board

'''


def getChipsFreq(ip, board=0):
    try:
        # get number of chips on board
        getNumbChips = {"command": "frequencyget", "parameter": str(board)}
        getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)
        while not tcp_connect.commandSuccessful(getNumbChipsResult):
            getNumbChipsResult = tcp_connect.send_cmd(getNumbChips, ip)

        return getNumbChipsResult['FREQS'][0]['Freqs']


    except Exception as e:
        log.error("getChipsFreq resulted in unknown error: " + str(e))
        return False


'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: which board to set frequency of chips 
freq: the freq to set the chip to
chip: which chip to apply the new freq to

Returns true if successful
'''


def setChipFreq(ip, board, freq, chip):
    try:
        if abs(getChipsFreq(ip, board)[chip] - freq) < 5:
            return True
        # Open session
        sessionID = tcp_connect.openSession(ip)

        parameter = sessionID + "," + str(board) + "," + str(freq) + "," + str(chip)
        setfreqCMD = {"command": "frequencyset", "parameter": parameter}
        setfreqResult = tcp_connect.send_cmd(setfreqCMD, ip)

        while not tcp_connect.chipSetSuccessful(setfreqResult, freq, chip) and abs(getChipsFreq(ip, board)[chip] - freq) > 5:
            tcp_connect.closeSession(ip, sessionID)
            sessionID = tcp_connect.openSession(ip)
            parameter = sessionID + "," + str(board) + "," + str(freq) + "," + str(chip)
            setfreqCMD = {"command": "frequencyset", "parameter": parameter}
            time.sleep(0.1)
            setfreqResult = tcp_connect.send_cmd(setfreqCMD, ip)

        # close session
        tcp_connect.closeSession(ip, sessionID)

        return True

    except Exception as e:
        log.error("setChipFreq resulted in unknown error: " + str(e))
        return False

'''
ip: the ip address of a miner e.g. '192.168.1.1' as a string
board: which board to set frequency of chips 
freqArray: array of frequencies to set the chips to

Returns true if successful
'''


def setChipFreqArray(ip, board, freqArray):
    try:
        currentChipsFreq = getChipsFreq(ip, board)
        while currentChipsFreq != freqArray:
            # Open session
            sessionID = tcp_connect.openSession(ip)
            for chip in range(len(freqArray)):
                if currentChipsFreq[chip] != freqArray[chip]:
                    parameter = sessionID + "," + str(board) + "," + str(freqArray[chip]) + "," + str(chip)
                    setfreqCMD = {"command": "frequencyset", "parameter": parameter}
                    setfreqResult = tcp_connect.send_cmd(setfreqCMD, ip)

            # close session
            tcp_connect.closeSession(ip, sessionID)

            currentChipsFreq = getChipsFreq(ip, board)

        return True

    except Exception as e:
        log.error("setChipFreq resulted in unknown error: " + str(e))
        return False


'''
Resets luxminer

ip: the ip address of a miner e.g. '192.168.1.1' as a string

Returns true if successful
'''


def resetLux(ip):
    try:
        # Open session
        sessionID = tcp_connect.openSession(ip)

        resetCMD = {"command": "resetminer", "parameter": sessionID}
        resetResult = tcp_connect.send_cmd(resetCMD, ip)

        #session is automatically closed

        while not tcp_connect.commandSuccessful(resetResult):
            resetResult = tcp_connect.send_cmd(resetCMD, ip)
        #wait for luxminer to come back online before proceeding
        time.sleep(300)
        return True

    except Exception as e:
        log.error("resetLux resulted in unknown error: " + str(e))
        return False


'''
Reboots miner

ip: the ip address of a miner e.g. '192.168.1.1' as a string

Returns true if successful
'''


def rebootMiner(ip):
    try:
        # Open session
        sessionID = tcp_connect.openSession(ip)

        resetCMD = {"command": "rebootdevice", "parameter": sessionID}
        resetResult = tcp_connect.send_cmd(resetCMD, ip)

        # session is automatically closed

        while not tcp_connect.commandSuccessful(resetResult):
            resetResult = tcp_connect.send_cmd(resetCMD, ip)
        # wait 5 min for luxminer to come back online before proceeding
        time.sleep(60 * 5)
        return True

    except Exception as e:
        log.error("rebootMiner resulted in unknown error: " + str(e))
        return False


'''
Gets board temps

ip: the ip address of a miner e.g. '192.168.1.1' as a string

returns all temps in an array
'''


def getTemps(ip):
    try:
        # get number of chips on board
        getTmps = {"command": "temps"}
        getTmpsResult = tcp_connect.send_cmd(getTmps, ip)
        while not tcp_connect.commandSuccessful(getTmpsResult):
            getTmpsResult = tcp_connect.send_cmd(getTemps, ip)

        temps = []

        for board in getTmpsResult['TEMPS']:
            temps.append(int(board['TopLeft']))
            temps.append(int(board['TopRight']))
            temps.append(int(board['BottomLeft']))
            temps.append(int(board['BottomRight']))

        return temps

    except Exception as e:
        log.error("getChipsFreq resulted in unknown error: " + str(e))
        return False


'''
Sets power mode

ip: the ip address of a miner e.g. '192.168.1.1' as a string
mode: either sleep or wakeup as a string

returns whether command was successful or not
'''


def setCurtailment(ip, mode):
    try:
        # Open session
        sessionID = tcp_connect.openSession(ip)

        parameter = sessionID + "," + mode
        setmodeCMD = {"command": "curtail", "parameter": parameter}
        setmodeResult = tcp_connect.send_cmd(setmodeCMD, ip)
        count = 0

        while not tcp_connect.commandSuccessful(setmodeResult):
            setmodeResult = tcp_connect.send_cmd(setmodeCMD, ip)
            count += 1
            if count > RETRIES:
                log.error('Failed to set curtailment mode')
                break

        # close session
        tcp_connect.closeSession(ip, sessionID)

        return True

    except Exception as e:
        log.error("setChipFreq resulted in unknown error: " + str(e))
        return False
