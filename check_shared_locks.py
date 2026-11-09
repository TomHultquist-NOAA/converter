#!/usr/bin/env python3
"""Non-destructive two-host check for filesystem flock interoperability.

On host A: python3 check_shared_locks.py hold /path/to/converter
On host B: python3 check_shared_locks.py probe /path/to/converter
Run probe while hold is active. The probe MUST report BUSY. Tests only a
separate diagnostic lock; never changes operational locks or data.
"""
import argparse
import fcntl
from pathlib import Path
import socket
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['hold','probe'])
    parser.add_argument('root',type=Path)
    parser.add_argument('--seconds',type=int,default=90)
    args=parser.parse_args()
    if not args.root.is_dir():parser.error('Shared directory missing')
    if not 1 <= args.seconds <= 600:parser.error('Hold seconds must be 1..600')
    path=args.root/'.evu_cross_node_lock_test.lock'
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('BUSY — another host/process holds the test lock (expected on probe host)')
            return 0 if args.mode=='probe' else 2
        if args.mode=='probe':
            print('UNSAFE — probe acquired the lock; ensure hold is active on another node')
            return 1
        print('HOLDING test lock on '+socket.gethostname()+' for '+str(args.seconds)+' seconds',flush=True)
        time.sleep(args.seconds)
        print('RELEASED')
    return 0

if __name__=='__main__':raise SystemExit(main())
