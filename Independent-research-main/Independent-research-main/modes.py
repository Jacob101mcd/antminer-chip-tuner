import commands
import logging
import time
import config
import copy
import numpy as np
import pickle


'''
Max efficiency tune
'''


def efficiencyTune(minerIP):

    # STEP 1
    info = commands.getLimits(minerIP)
    minVoltage = info[0]
    defaultVoltage = info[1]
    maxVoltage = info[2]
    voltageStep = info[3]
    minFreq = info[4]
    defaultFreq = info[5]
    maxFreq = info[6]
    freqStep = info[7]

    # STEP 2

    # Set overrides if any
    if config.MIN_FREQ != 0:
        minFreq = config.MIN_FREQ
    if config.MIN_VOLT != 0:
        minVoltage = config.MIN_VOLT

    # STEP 3
    # Set voltage to min
    commands.setVoltage(minerIP, minVoltage)

    # Set freq to min on all boards
    commands.setBoardFreq(minerIP, minFreq, 0)
    commands.setBoardFreq(minerIP, minFreq, 1)
    commands.setBoardFreq(minerIP, minFreq, 2)

    # wait until min voltage and frequency are reached and chips have settled
    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Voltage: " + str(commands.getVoltage(minerIP)) + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Freq: " + str(commands.getAverageMinerFreq(minerIP)) + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Chip Health: " + str(commands.chipsHealthKnown(minerIP)) + '\n')
    f.close()

    while not (abs(commands.getVoltage(minerIP) - minVoltage) < 0.25) or not \
            (abs(commands.getAverageMinerFreq(minerIP) - minFreq) < 10) or not (commands.chipsHealthKnown(minerIP)):
        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + '\n\nNot stable yet' + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Voltage: " + str(commands.getVoltage(minerIP)) + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Freq: " + str(commands.getAverageMinerFreq(minerIP)) + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Chip Health: " + str(commands.chipsHealthKnown(minerIP)) + '\n')
        f.close()
        time.sleep(30)

    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'miner settled' + '\n')
    f.close()

    # STEP 4
    board0StableFreqArray = commands.getChipsFreq(minerIP, 0)
    board1StableFreqArray = commands.getChipsFreq(minerIP, 1)
    board2StableFreqArray = commands.getChipsFreq(minerIP, 2)

    proposedFreq0 = commands.getChipsFreq(minerIP, 0)
    proposedFreq1 = commands.getChipsFreq(minerIP, 1)
    proposedFreq2 = commands.getChipsFreq(minerIP, 2)

    # Get baseline scores
    baselineScore_0 = np.array(commands.getChipsScore(minerIP, 0))
    baselineScore_1 = np.array(commands.getChipsScore(minerIP, 1))
    baselineScore_2 = np.array(commands.getChipsScore(minerIP, 2))

    for x in range(9):
        time.sleep(5)
        baselineScore_0 = np.add(baselineScore_0, commands.getChipsScore(minerIP, 0))
        baselineScore_1 = np.add(baselineScore_1, commands.getChipsScore(minerIP, 1))
        baselineScore_2 = np.add(baselineScore_2, commands.getChipsScore(minerIP, 2))

    baselineScore_0 = [divmod(x, 10)[0] for x in baselineScore_0]
    baselineScore_1 = [divmod(x, 10)[0] for x in baselineScore_1]
    baselineScore_2 = [divmod(x, 10)[0] for x in baselineScore_2]

    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Baseline Scores: ' + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(baselineScore_0) + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(baselineScore_1) + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(baselineScore_2) + '\n')
    f.close()

    changed = True

    while changed is True:

        # Get chip health scores
        changed = False
        board0HealthArray = commands.getChipsScore(minerIP, 0)
        board1HealthArray = commands.getChipsScore(minerIP, 1)
        board2HealthArray = commands.getChipsScore(minerIP, 2)

        for x in range(9):
            time.sleep(5)
            board0HealthArray = np.add(board0HealthArray, commands.getChipsScore(minerIP, 0))
            board1HealthArray = np.add(board1HealthArray, commands.getChipsScore(minerIP, 1))
            board2HealthArray = np.add(board2HealthArray, commands.getChipsScore(minerIP, 2))

        board0HealthArray = [divmod(x, 10)[0] for x in board0HealthArray]
        board1HealthArray = [divmod(x, 10)[0] for x in board1HealthArray]
        board2HealthArray = [divmod(x, 10)[0] for x in board2HealthArray]

        # Find max freq of chips board 0
        for numb, score in enumerate(board0HealthArray):
            if score >= baselineScore_0[numb] - 10:
                proposedFreq0[numb] += 5
                if proposedFreq0[numb] > board0StableFreqArray[numb] and proposedFreq0[numb] - min(proposedFreq0) < config.MAX_SPREAD:
                    # Found new max, save and increase
                    board0StableFreqArray[numb] = proposedFreq0[numb]
                    changed = True
                else:
                    # revert to stable freq
                    proposedFreq0[numb] -= 5
            else:
                # Chip is unstable, lower freq back to prev value
                proposedFreq0[numb] -= 10

        # Find max freq of chips board 1
        for numb, score in enumerate(board1HealthArray):
            if score >= baselineScore_1[numb] - 10:
                proposedFreq1[numb] += 5
                if proposedFreq1[numb] > board1StableFreqArray[numb] and proposedFreq1[numb] - min(proposedFreq1) < config.MAX_SPREAD:
                    # Found new max, save and increase
                    board1StableFreqArray[numb] = proposedFreq1[numb]
                    changed = True
                else:
                    # revert to stable freq
                    proposedFreq1[numb] -= 5
            else:
                # Chip is unstable, lower freq back to prev value
                proposedFreq1[numb] -= 10

        # Find max freq of chips board 2
        for numb, score in enumerate(board2HealthArray):
            if score >= baselineScore_2[numb] - 10:
                proposedFreq2[numb] += 5
                if proposedFreq2[numb] > board2StableFreqArray[numb] and proposedFreq2[numb] - min(proposedFreq2) < config.MAX_SPREAD:
                    # Found new max, save and increase
                    board2StableFreqArray[numb] = proposedFreq2[numb]
                    changed = True
                else:
                    # revert to stable freq
                    proposedFreq2[numb] -= 5
            else:
                # Chip is unstable, lower freq back to prev value
                proposedFreq2[numb] -= 10
                # Maintain old highest frequency

        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Proposed frequencies: ' + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(proposedFreq0) + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(proposedFreq1) + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + str(proposedFreq2) + '\n')
        f.close()

        # Curtail miner to reset chips
        commands.setCurtailment(minerIP, 'sleep')
        time.sleep(30)
        commands.setCurtailment(minerIP, 'wakeup')
        time.sleep(60)

        # Wait for chips to stabilize after reset
        while not commands.chipsHealthKnown(minerIP):
            time.sleep(10)

        # set mode values to accelerate tuning process
        commands.setVoltage(minerIP, minVoltage)
        commands.setBoardFreq(minerIP, min(proposedFreq0), 0)
        commands.setBoardFreq(minerIP, min(proposedFreq1), 1)
        commands.setBoardFreq(minerIP, min(proposedFreq2), 2)

        # Wait for chips to stabilize
        while not commands.chipsHealthKnown(minerIP):
            time.sleep(10)

        # Try to set proposed frequencies for each chip
        if min(proposedFreq0) != max(proposedFreq0):
            # If some chips differ, set freq chip by chip
            for numb, freq in enumerate(proposedFreq0):
                # try to set stable freq
                commands.setChipFreq(minerIP, 0, freq, numb)

        # Try to set proposed frequencies for each chip
        if min(proposedFreq1) != max(proposedFreq1):
            # If some chips differ, set freq chip by chip
            for numb, freq in enumerate(proposedFreq1):
                # try to set stable freq
                commands.setChipFreq(minerIP, 1, freq, numb)

        # Try to set proposed frequencies for each chip
        if min(proposedFreq2) != max(proposedFreq2):
            # If some chips differ, set freq chip by chip
            for numb, freq in enumerate(proposedFreq2):
                # try to set stable freq
                commands.setChipFreq(minerIP, 2, freq, numb)

        # Wait for chips to stabilize
        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Waiting for chips to stabilize' + '\n')
        while not commands.chipsHealthKnown(minerIP):
            f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Still waiting for chips to stabilize' + '\n')
            time.sleep(10)
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Chips stabilized' + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + '\nTuning round complete!' + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Freq: " + str(commands.getAverageMinerFreq(minerIP)) + '\n')
        f.close()


    # Tune done, apply last stable freq
    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Tune Done!' + '\n')
    f.close()

    # Save last stable freq
    filename = minerIP.replace('.', '-') + '.pk'
    with open(filename, 'wb') as voltage:
        # dump your data into the file
        pickle.dump(minVoltage, voltage)

    with open(filename, 'wb') as board0Score:
        # dump your data into the file
        pickle.dump(baselineScore_0, board0Score)
    with open(filename, 'wb') as board1Score:
        # dump your data into the file
        pickle.dump(baselineScore_1, board1Score)
    with open(filename, 'wb') as board2Score:
        # dump your data into the file
        pickle.dump(baselineScore_2, board2Score)

    with open(filename, 'wb') as board0:
        # dump your data into the file
        pickle.dump(board0StableFreqArray, board0)
    with open(filename, 'wb') as board1:
        # dump your data into the file
        pickle.dump(board1StableFreqArray, board1)
    with open(filename, 'wb') as board0:
        # dump your data into the file
        pickle.dump(board0StableFreqArray, board0)

    # Tune done, apply last stable freq
    # Tune done, apply last stable freq
    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Tune Saved' + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Entering Perpetual tune' + '\n')
    f.close()

    perpetualTune(minerIP)

'''
perpetual tune mode
'''

def perpetualTune(minerIP):
    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Entering perpetual tune' + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Loading saved data...' + '\n')
    f.close()

    minVoltage = 0
    baselineScore_0 = []
    baselineScore_1 = []
    baselineScore_2 = []
    board0StableFreqArray = []
    board1StableFreqArray = []
    board2StableFreqArray = []

    filename = minerIP.replace('.', '-') + '.pk'

    try:
        # load your data back to memory
        with open(filename, 'rb') as voltage:
            minVoltage = pickle.load(voltage)

        with open(filename, 'rb') as board0Score:
            baselineScore_0 = pickle.load(board0Score)
        with open(filename, 'rb') as board1Score:
            baselineScore_1 = pickle.load(board1Score)
        with open(filename, 'rb') as board2Score:
            baselineScore_2 = pickle.load(board2Score)

        with open(filename, 'rb') as board0:
            board0StableFreqArray = pickle.load(board0)
        with open(filename, 'rb') as board1:
            board1StableFreqArray = pickle.load(board1)
        with open(filename, 'rb') as board2:
            board2StableFreqArray = pickle.load(board2)

        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Data loaded successfully!' + '\n')
        f.close()

    except:
        # If no saved data, start from scratch
        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'No saved data found, starting tune from scratch' + '\n')
        f.close()
        efficiencyTune(minerIP)

    for numb, freq in enumerate(board0StableFreqArray):
        # try to set stable freq
        commands.setChipFreq(minerIP, 0, freq, numb)

    for numb, freq in enumerate(board1StableFreqArray):
        # try to set stable freq
        commands.setChipFreq(minerIP, 1, freq, numb)

    for numb, freq in enumerate(board2StableFreqArray):
        # try to set stable freq
        commands.setChipFreq(minerIP, 2, freq, numb)

    f = open(minerIP + "-log.txt", "a")
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'Applied stable freq' + '\n')
    f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Freq: " + str(commands.getAverageMinerFreq(minerIP)) + '\n')
    f.close()

    while True:
        # Wait for chips to stabilize
        while not commands.chipsHealthKnown(minerIP):
            time.sleep(10)

        changed = False
        board0HealthArray = commands.getChipsScore(minerIP, 0)
        board1HealthArray = commands.getChipsScore(minerIP, 1)
        board2HealthArray = commands.getChipsScore(minerIP, 2)

        for x in range(19):
            time.sleep(60)
            board0HealthArray = np.add(board0HealthArray, commands.getChipsScore(minerIP, 0))
            board1HealthArray = np.add(board1HealthArray, commands.getChipsScore(minerIP, 1))
            board2HealthArray = np.add(board2HealthArray, commands.getChipsScore(minerIP, 2))

        board0HealthArray = [divmod(x, 20)[0] for x in board0HealthArray]
        board1HealthArray = [divmod(x, 20)[0] for x in board1HealthArray]
        board2HealthArray = [divmod(x, 20)[0] for x in board2HealthArray]

        # Find max freq of chips board 0
        for numb, chip in enumerate(board0HealthArray):
            if baselineScore_0[numb] - chip > 20:
                # try new freq
                board0StableFreqArray[numb] -= 10
                changed = True
            if board0StableFreqArray[numb] - min(board0StableFreqArray) > config.MAX_SPREAD:
                # Spread too high, lower all highest chips
                board0StableFreqArray[numb] -= 10

        # Find max freq of chips board 1
        for numb, chip in enumerate(board1HealthArray):
            if baselineScore_1[numb] - chip > 20:
                # try new freq
                board1StableFreqArray[numb] -= 10
                changed = True
            if board1StableFreqArray[numb] - min(board1StableFreqArray) > config.MAX_SPREAD:
                # Spread too high, lower all highest chips
                board1StableFreqArray[numb] -= 10

        # Find max freq of chips board 2
        for numb, chip in enumerate(board2HealthArray):
            if baselineScore_2[numb] - chip > 20:
                # try new freq
                board2StableFreqArray[numb] -= 10
                changed = True
            if board2StableFreqArray[numb] - min(board2StableFreqArray) > config.MAX_SPREAD:
                # Spread too high, lower all highest chips
                board2StableFreqArray[numb] -= 10

        if changed:
            # Curtail miner to reset chips
            commands.setCurtailment(minerIP, 'sleep')
            time.sleep(30)
            commands.setCurtailment(minerIP, 'wakeup')
            time.sleep(60)

            # Wait for chips to stabilize
            f = open(minerIP + "-log.txt", "a")
            f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'waiting for chips to stabilize' + '\n')
            f.close()
            while not commands.chipsHealthKnown(minerIP):
                time.sleep(30)

            commands.setVoltage(minerIP, minVoltage)
            commands.setBoardFreq(minerIP, min(board0StableFreqArray), 0)
            commands.setBoardFreq(minerIP, min(board1StableFreqArray), 1)
            commands.setBoardFreq(minerIP, min(board2StableFreqArray), 2)

            # Wait for chips to stabilize
            f = open(minerIP + "-log.txt", "a")
            f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + 'waiting for chips to stabilize' + '\n')
            f.close()
            while not commands.chipsHealthKnown(minerIP):
                time.sleep(30)

            for numb, freq in enumerate(board0StableFreqArray):
                # try to set stable freq
                commands.setChipFreq(minerIP, 0, freq, numb)

            for numb, freq in enumerate(board1StableFreqArray):
                # try to set stable freq
                commands.setChipFreq(minerIP, 1, freq, numb)

            for numb, freq in enumerate(board2StableFreqArray):
                # try to set stable freq
                commands.setChipFreq(minerIP, 2, freq, numb)

        time.sleep(60 * config.PERPETUAL_TUNE_CHECK)

        f = open(minerIP + "-log.txt", "a")
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + '\nTuning round complete!' + '\n')
        f.write(str(time.strftime("%H:%M:%S", time.localtime())) + ': ' + "Current Freq: " + str(commands.getAverageMinerFreq(minerIP)) + '\n')
        f.close()

        
'''
gets temps and shuts down miner if over limit

returns an array of temps
'''


def safety(minerIP):
    temps = commands.getTemps(minerIP)

    try:
        if max(temps) >= config.MAX_TEMP:
            # over limit, shutdown
            commands.setCurtailment(minerIP, 'sleep')

            # Notify user
            current_time = time.strftime("%H:%M:%S", time.localtime())

            f = open(minerIP + "-log.txt", "a")
            f.write(str(current_time) + ': Miner at ' + str(minerIP) + ' has overheated at temperature ' + str(max(temps)) + ' and has been shut down!')
            f.close()
        return temps

    except Exception as e:
        return []

