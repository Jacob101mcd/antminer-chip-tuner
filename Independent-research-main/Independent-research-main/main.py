import keyboard
import config
import commands
import logging
import modes
import time
import ipscanner
from joblib import Parallel, delayed
import os
import stopit
import concurrent.futures

compatibleMiners = ipscanner.getIPs()

mode = int(input('Perpetual Tune Mode = 1, Monitor and Tune Mode = 2\n'))

#Tune mode
if mode == 1:
    parallel = Parallel(n_jobs=-1, return_as="generator")
    output_generator = list(parallel(delayed(modes.perpetualTune)(minerIP) for minerIP in compatibleMiners))


# Monitor:
if mode == 2:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Run tuning process in the background
        future_to_task = {executor.submit(modes.efficiencyTune, minerIP): minerIP for minerIP in compatibleMiners}

        history = ''

        count = 0
        while True:
            time.sleep(1)

            parallel = Parallel(n_jobs=-1, return_as="generator")

            # timeout for retrieving temps
            with stopit.ThreadingTimeout(5) as context_manager:
                output_generator = list(parallel(delayed(modes.safety)(minerIP) for minerIP in compatibleMiners))
            if context_manager.state == context_manager.EXECUTED:
                temps = []
                for thread in output_generator:
                    for t in thread:
                        temps.append(t)

                if len(temps) < 1:
                    temps.append(0)

                # Safety Mechanism
                #if max(temps) >= config.MAX_TEMP:
                    # A miner has overheated, curtail all miners
                #    history += '\n' + str(
                #        time.strftime("%H:%M:%S", time.localtime())) + ': A miner has overheated at temp ' + str(
                #        max(temps)) + ', curtailing all units. When issue has been resolved, hold w key to resume mining\n'
                #    output_generator = list(
                #        parallel(delayed(commands.setCurtailment)(minerIP, 'sleep') for minerIP in compatibleMiners))

                highTemp = 'Highest temp: ' + str(max(temps)) + ' C'
                avgTemp = 'Average temp: ' + str(int(sum(temps) / len(temps))) + ' C'
                lowTemp = 'Lowest temp: ' + str(min(temps)) + ' C'

                if keyboard.is_pressed("s"):
                    count += 1
                    if count >= 60:
                        count = 0
                        history += '\n\n\n' + str(
                            time.strftime("%H:%M:%S", time.localtime())) + ': Manual shutdown of miners is starting...'
                        output_generator1 = list(parallel(
                            delayed(commands.setCurtailment)(minerIP, 'sleep') for minerIP in compatibleMiners))

                        success = 0
                        fail = 0
                        for attempt in enumerate(output_generator1):
                            if attempt[1]:
                                success += 1
                            else:
                                fail += 1
                                history += '\nMiner with ip ' + str(compatibleMiners[attempt[0]]) + ' refused command'
                        history += 'Shutdown Complete! \n' + str(success) + ' miner(s) complied, ' + str(
                            fail) + ' miner(s) refused'
                elif keyboard.is_pressed("w"):
                    count += 1
                    if count >= 60:
                        count = 0
                        history += '\n\n\n' + str(
                            time.strftime("%H:%M:%S", time.localtime())) + ': Manual startup of miners is starting...'
                        output_generator2 = list(parallel(
                            delayed(commands.setCurtailment)(minerIP, 'wakeup') for minerIP in compatibleMiners))

                        success = 0
                        fail = 0
                        for attempt in enumerate(output_generator2):
                            if attempt[1]:
                                success += 1
                            else:
                                fail += 1
                                history += '\nMiner with ip ' + str(compatibleMiners[attempt[0]]) + ' refused command'
                        history += 'Startup Complete! \n' + str(success) + ' miner(s) complied, ' + str(
                            fail) + ' miner(s) refused'
                elif keyboard.is_pressed("m"):
                    count += 1
                    if count >= 50:
                        count = 0
                        history = ''
                elif keyboard.is_pressed("n"):
                    count += 1
                    if count >= 50:
                        count = 0
                        number = len(compatibleMiners)
                        os.system('clear')
                        print('\n\n\nScanning, please wait....')
                        history += '\n\nScanning...'
                        compatibleMiners = ipscanner.getAliveIPs()
                        history += 'Scan Complete!'
                        history += '\nNumber of miners changed by ' + str(number - len(compatibleMiners)) + '\n'
                else:
                    count = 0

                os.system('clear')
                print('Local Time: ' + str(time.strftime("%H:%M:%S", time.localtime())))
                #print('\n\nTo manually pause miners, hold the \"S\" key for 3 seconds.')
                #print('To manually resume mining, hold the \"W\" key for 3 seconds.\n')
                print('To rescan the network for miners after network changes, hold the \"N\" key')
                print('To clear the log, hold the \"M\" key\n')

                print('Number of miners monitored: ' + str(len(compatibleMiners)))
                print(highTemp)
                print(avgTemp)
                print(lowTemp)

                print(history)

            else:
                # timeout on temps, network change? Rescan network!
                compatibleMiners = ipscanner.getAliveIPs()

