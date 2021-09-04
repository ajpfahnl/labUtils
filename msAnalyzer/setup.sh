#!/bin/bash

python3 -m venv venv
source ./venv/bin/activate
pip install -r requirements.txt
chmod u+x setup.sh run.sh

printf "
Activate the virtual environment by entering the following command:
\tsource venv/bin/activate
then run msAnalyzer with
\tpython msAnalyzer.py

Alternatively, run both steps above with
\t./run.sh
"
