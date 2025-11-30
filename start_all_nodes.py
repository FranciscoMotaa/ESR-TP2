#!/usr/bin/env python3
import json
import subprocess
import time
import os
import signal

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, 'bootstrap_conf.json')
LOGDIR = os.path.join(ROOT, 'logs')

TRACKER_CMD = ['python3', os.path.join(ROOT, 'bootstrapper.py')]
NODE_CMD = ['python3', os.path.join(ROOT, 'main.py')]

# Tracker bind port is 6000 in bootstrapper.py
TRACKER_IP = '127.0.0.1'

def start_tracker():
    os.makedirs(LOGDIR, exist_ok=True)
    tracker_log = open(os.path.join(LOGDIR, 'bootstrapper.log'), 'w')
    print('[*] Iniciando Bootstrapper (tracker) ...')
    proc = subprocess.Popen(TRACKER_CMD, stdout=tracker_log, stderr=subprocess.STDOUT)
    print(f'    PID tracker: {proc.pid}  (log: {tracker_log.name})')
    return proc, tracker_log

def start_node(node_id, tracker_ip, logfile=None):
    if logfile is None:
        logfile = os.path.join(LOGDIR, f'{node_id}.log')
    lf = open(logfile, 'w')
    cmd = NODE_CMD + [node_id, '--tracker', tracker_ip]
    print(f'[*] A iniciar nó {node_id} ...')
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    print(f'    PID {node_id}: {p.pid} (log: {logfile})')
    return p, lf


if __name__ == '__main__':
    # 1) Start tracker (bootstrapper should run on the host representing R3)
    tracker_proc, tracker_log = start_tracker()
    # Give tracker a moment to start
    time.sleep(1.0)

    # 2) Load config
    with open(CONF, 'r') as f:
        cfg = json.load(f)

    nodes = cfg.get('nodes') or cfg

    # Determine R7 (the node that must register first)
    first_node_id = 'R7'
    procs = []

    # 3) If R7 exists in config, start it first so it registers to the tracker
    r7_entry = next((n for n in nodes if n.get('id') == first_node_id), None)
    if r7_entry:
        p, lf = start_node(first_node_id, TRACKER_IP)
        procs.append((first_node_id, p, lf))
        # Wait a bit to allow R7 to register via TCP
        time.sleep(2.0)

    # 4) Start remaining nodes (including R3) after R7 has had time to register
    for node in nodes:
        node_id = node.get('id')
        if not node_id:
            continue
        if node_id == first_node_id:
            continue

        p, lf = start_node(node_id, TRACKER_IP)
        procs.append((node_id, p, lf))
        time.sleep(0.3)

    print('\n[*] Todos os processos lançados.')
    print('Lista de processos:')
    for node_id, p, lf in procs:
        print(f'  - {node_id}: PID {p.pid} log={lf.name}')

    print('\nPara parar: execute os comandos abaixo (a partir do workspace):')
    print('  kill', tracker_proc.pid)
    for node_id, p, lf in procs:
        print('  kill', p.pid)

    try:
        # Wait indefinitely, forward Ctrl+C
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[*] A terminar processos ...')
        try:
            tracker_proc.terminate()
        except: pass
        for node_id, p, lf in procs:
            try:
                p.terminate()
            except: pass
            try:
                lf.close()
            except: pass
        try:
            tracker_log.close()
        except: pass
        print('[*] Feito.')
