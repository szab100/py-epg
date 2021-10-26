# py_epg

**py_epg** is an easy to use, modular, multi-process EPG grabber written in Python.

* 📺 Scrapes various TV Program websites and saves programs in XMLTV format.
* 🧩 Simply extend [EpgScraper](https://github.com/szab100/py_epg/blob/main/py_epg/common/epg_scraper.py) to grab EPG from your favorite TV site (requires basic Python skills).
* 🤖 The framework provides the rest:
    * [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/doc) - easily search & extract data from html elements 
    * multi-processing
    * config management
    * logging
    * build & write XMLTV (with auto-generated fields, eg 'stop')
    * proxy server support
    * auto http/s retries
    * random fake user_agents
* 🚀 Save time by fetching channels in parallel (caution: use proxy server(s) to avoid getting blacklisted)!

<p align="center">
  <img src="https://raw.githubusercontent.com/szab100/py_epg/main/py_epg.gif">
</p>
## Install

1. Install poetry: 
    ```sh
    curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -
    ```

2. Clone repository & install dependencies:
      ```sh
      git clone https://github.com/szab100/py_epg.git
      ```

3. Configure py_epg.xml
    - Add all your channels as per the sample. Make sure you have a scraper implementation in py_epg/scrapers/ for each channels ('site' attribute).

4. Run:
      ```sh
      cd py_epg
      poetry run epg -c py_epg.xml
      ```
## Usage

```sh
$ poetry run epg -h
usage: epg [-h] [-p [PROGRESS_BAR]] [-q [QUIET]] -c CONFIG

A simple, multi-threaded, modular EPG grabber written in Python

optional arguments:
-h, --help            show this help message and exit
-p [PROGRESS_BAR], --progress-bar [PROGRESS_BAR]
                        Show a progress bar. Default: True
-q [QUIET], --quiet [QUIET]
                        Quiet mode (no progress-bar, etc). Default: False

required arguments:
-c CONFIG, --config CONFIG
                        Path to py_epg.xml file
```

## License

Copyright 2021. Released under the MIT license.
