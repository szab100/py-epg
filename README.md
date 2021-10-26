# py-epg

**py-epg** is an easy to use, modular, multi-process EPG grabber written in Python.

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
* 🧑🏻‍💻 Your contributions are welcome! Feel free to create a PR with your tv-site scraper and/or framework improvements.

<p align="center">
  <img src="https://raw.githubusercontent.com/szab100/py_epg/main/py_epg.gif">
</p>

## Usage

1. Install package:
    ```sh
    $ pip3 install py_epg
    ```
2. Create configuration: py_epg.xml
    - Add all your channels (see [sample config](https://github.com/szab100/py-epg/blob/main/py_epg.xml)).
    - Make sure there is a corresponding site scraper implementation in [py_epg/scrapers](https://github.com/szab100/py-epg/tree/main/py_epg/scrapers) for each channels ('site' attribute).
3. Run:
    ```sh
    $ python3 -m py_epg -c </path/to/your/py_epg.xml>
    ```

    ..or see all supported flags:
    ```sh
    $ python3 -m py_epg -h
    usage: py_epg [-h] [-p [PROGRESS_BAR]] [-q [QUIET]] -c CONFIG
    ...
    ```

## Development

Your contributions are welcome! Setup your dev environment as described below. [VSCode](https://code.visualstudio.com/) is a great free IDE for python projects. Once you are ready with your cool tv site scraper or framework feature, feel free to open a Pull Request here.

1. Install poetry: 
    ```sh
    curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -
    ```

2. Clone repository & install dependencies:
      ```sh
      git clone https://github.com/szab100/py-epg.git && cd py-epg
      ```

3. Configure py_epg.xml
    - Add all your channels (see the sample config xml). Make sure you have a scraper implementation in py_epg/scrapers/ for each channels ('site' attribute).

4. Run:
      ```sh
      poetry install
      poetry run epg -c py_epg.xml
      ```

## License

Copyright 2021. Released under the MIT license.
