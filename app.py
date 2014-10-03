#!/usr/bin/env python
# -*- coding: utf8 -*-
"""
ScrapeStream

App to routinely collect data from various sources and pushe to
a websocket.
"""

import gevent.monkey
gevent.monkey.patch_all()

import gdbm
import hashlib
import json
import signal
import time
import uuid

import feedparser
import gevent
import requests

from bs4 import BeautifulSoup
from gevent.queue import Queue
from geventwebsocket import WebSocketServer, WebSocketApplication, Resource


#-----------------------------------------------------------------------
# Set up
#-----------------------------------------------------------------------
db = gdbm.open('db', 'cf')  # Mostly just saves state if the app restart
                            # to save flooding users with old feeds
queue = Queue()             # Transport from workers to websocket
allowed_parsers = ['rss',
                   'html']  # Available parsers

# Load the config from a json file
with open('sites.json', 'r') as content_file:
    sites = json.loads(content_file.read())


#-----------------------------------------------------------------------
# Parsers
#-----------------------------------------------------------------------
def rss_parser(site):
    """Basic RSS parser"""
    print "Checking: %s" % site['url']
    d = feedparser.parse(site['url'])
    f = d.feed
    id_last_updated = site['id']+"::last_updated"
    last_updated = float(db[id_last_updated])

    if float(time.mktime(f.updated_parsed)) > last_updated:
        for entry in d.entries:
            if float(time.mktime(entry.updated_parsed)) > last_updated:
                msg = json.dumps({
                    'site': d.feed.title,
                    'title': entry.title,
                    'link': entry.link,
                    'date': entry.published,
                    'summary': entry.description
                })
                queue.put(msg)
                print d.feed.title
                gevent.sleep(0.1)
                del(msg)
    db[id_last_updated] = str(time.mktime(d.feed.updated_parsed))
    db.sync()
    del(d)
    return


def html_parser(site):
    """Basic HTML parser"""
    print "Checking: %s" % site['url']

    r = requests.get(site['url'])
    soup = BeautifulSoup(r.content)
    tags = site.get('tags')

    if not tags.get('section_wrap'):
        return

    container = soup.select(tags.get('section_wrap'))[0]

    for item in container.select(tags['item_wrap']):
        p = {'site':  site['name']}
        try:
            p['title'] = str(item.select(tags['title'])[0].text) or ''
        except Exception:
            p['title'] = ''
        try:
            p['link'] = str(item.select(tags['link'])[0].get('href', ''))
            if p['link'][0] == '/':
                p['link'] = site['url_root'] + p['link']
        except Exception:
            p['link'] = ''
        try:
            p['date'] = str(item.select(tags['date'])[0])
        except Exception:
            p['date'] = ''
        try:
            p['summary'] = str(item.select(tags['summary'])[0])
        except Exception:
            p['summary'] = ''

        # As we can’t rely on there being a date, we need to store at
        # least as many previous items as displayed on the site. 100
        # feels more than enough. Use the md5 to make things easier.
        p = json.dumps(p)
        phash = hashlib.md5(p).hexdigest()
        try:  # If no previous value exists
            previous = json.loads(db[site['id']+'::hashes'])
        except KeyError:
            previous = []
        if phash in previous:  # We already got it!
            return

        if len(previous) >= 100:
            del(previous[0])
        previous.append(phash)

        db[site['id']+'::hashes'] = json.dumps(previous)
        db.sync()
        print site['name']
        queue.put(p)
        gevent.sleep(0.1)
        del(p)
    del(container)
    return


#-----------------------------------------------------------------------
# Main functions
#-----------------------------------------------------------------------
def worker(site):
    """A single worker, checks the site & adds new results to the queue"""
    if site['parser'] not in allowed_parsers:  # Are we allowed to run it?
        return

    parser = str(site['parser']+'_parser')
    site['id'] = str(uuid.uuid3(uuid.NAMESPACE_URL, str(site['url'])))

    try:
        db[site['id']+"::last_updated"]
    except KeyError:
        db[site['id']+"::last_updated"] = '0'

    while True:
        globals()[parser](site)
        gevent.sleep(site['rate']*60)  # Rate is in minutes


def async_workers():
    """Starts up all the workers"""
    gevent.sleep(2)  # give collector & websocket time to start up
    workers = [gevent.spawn(worker, site) for site in sites]
    gevent.joinall(workers)


def collector(server):
    """Listens to the queue & pushes to all open sockets"""
    while True:
        post = queue.get()
        gevent.sleep(0.1)
        for sessid, socket in server.clients.iteritems():
            socket.ws.send(post)


#-----------------------------------------------------------------------
# Websockets
#-----------------------------------------------------------------------
class StreamApp(WebSocketApplication):
    """Deal with websockets"""
    def on_open(self):
        print "Connection opened."

    def on_close(self, reason):
        print "Connection Closed!!!", reason


resource = Resource({
    '/': StreamApp,
})

gevent.spawn(async_workers)

if __name__ == "__main__":
    print 'starting…'
    server = WebSocketServer(('127.0.0.1', 8000), resource, debug=True)
    gevent.signal(signal.SIGQUIT, gevent.kill)
    gevent.spawn(collector, server)

    server.serve_forever()
