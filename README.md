
# rss/atom to ReMarkable Cloud

This program turns your list of RSS feeds into a set of PDFs, then syncs them with your Remarkable.

Let it run using cron to update daily.

## Install

### rMAPI

You need to install rmapi to authenticate with the ReMarkable cloud (or a fake cloud if you have one):

https://github.com/ddvk/rmapi

Then use rmapi to authenticate:

```bash
rmapi 
# It will ask you for the code shown in your remarkable account
```

### Python libs

```bash
# optional: make virtual environment for python libs
python -m venv venv
source venv/bin/activate

# install requirements
pip install -r requirements.txt
```

## Usage

### input.toml (RSS/Atom feeds)

Put the rss feeds into the `input.toml` file like this:

```toml
[orf]
orf_news = "https://rss.orf.at/news.xml"

[it-news]
ars_technica = "http://feeds.arstechnica.com/arstechnica/index/"
lwn = "https://lwn.net/headlines/newrss"          # kernel/Linux, unusually deep
lobsters = "https://lobste.rs/rss"                # smaller, less noisy than HN
changelog = "https://changelog.com/feed"

    [it-news.hackernews]
    main = "https://news.ycombinator.com/rss"
    best = "https://hnrss.org/best"
    top100 = "https://hnrss.org/frontpage?points=100" # threshold filter
    daily_top10 = "https://www.daemonology.net/hn-daily/index.rss"
```

### Let it spin

```bash
# 1) run
python rss2remarkable.py
# 2) ?????
echo "moo"
# 3) profit!
```
