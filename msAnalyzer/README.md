# msAnalyzer
## Setup
Install the necessary packages in a virtual environment (venv) by running:
```shell
bash setup.sh
```
## Running the program
Running the program is as simple as:
```
./run.sh
```
`run.sh` does the following:
1. Activate the virtual environment by entering the following command:
    ```shell
    source venv/bin/activate
    ```
2. Then run msAnalyzer with
    ```shell
    python msAnalyzer.py
    ```
## Troubleshooting
If one of the scripts isn't running properly, try manually enabling executable privileges with
```
chmod u+x setup.sh run.sh
```