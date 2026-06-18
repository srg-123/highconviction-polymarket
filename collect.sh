#!/bin/bash
# Daily ATP price history collector
cd /Users/srkr_g/Documents/pm_backtester
/opt/homebrew/bin/python3 data_collector.py >> logs/collector.log 2>&1
