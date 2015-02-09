#!/usr/bin/python
#
#
#
#
#
#

import logging
import re
import tempfile

from curl import Curl
from HTMLParser import HTMLParser
from urllib import quote

from bson.son import SON
from pymongo.son_manipulator import SONManipulator
from pymongo import MongoClient, ASCENDING as ASC, DESCENDING as DESC

from irc.bot import Channel
from irc.dict import IRCDict

from oyoyo.client import IRCClient
from oyoyo.cmdhandler import DefaultCommandHandler
from oyoyo import helpers

# precompiled regex for the bot's commands; should probably put these in a separate file
cmd = re.compile("\.(?P<command>\w+)( (?P<args>.*)|)")
recd = re.compile("(\-(?P<lines>\d+)(\!(?P<except>[0-9,]+)|) )(?P<target>.*) \=> (?P<recipe>.*)")
conv = re.compile("(?P<num>[0-9\.].*) (?P<unit1>[a-zA-Z].*) (to |\=> )(?P<unit2>[a-zA-Z].*)")
http = re.compile(".*(\http://|\https://)(?P<url>.*)")
crawl = re.compile(".*\<title>(?P<title>.*)\</title>.*", re.I | re.S)
mod = re.compile("(?P<mode1>(\+|\-)\w+)((?P<mode2>(\+|\-)\w+)|)")
addr = re.compile("(Marzipan|Pan)(:|,)(?P<args>.*)", re.I)
intro = re.compile("introduce yourself", re.I)
rem = re.compile("remember (?P<target>.*) (?P<phrase>.*)", re.I)
make = re.compile("make a (new |)pantry for (?P<user>.*)", re.I)
add = re.compile("add (?P<items>.*) to my pantry", re.I)
ls = re.compile("(list pantry|what's in my pantry(\?|))", re.I)
#ls2 = re.compile("list (?P<whos>.*) pantry", re.I)
rm = re.compile("remove (?P<items>.*) from my pantry", re.I)
cls = re.compile("clear my pantry", re.I)

vol = { # US measurements
        "tsp" : 1, # this dict uses teaspoons as a base
        "tbsp" : 3,
        "oz" : 6,
        "cups" : 48,
        "pt" : 96,
        "qt" : 192,
        "gal" : 768,
        # metric measurements
        "ml" : 0.20288414,
        "l" : 202.884136 }

wt = { # metric measurements
       "mg" : 1, # this one uses mg as a base
       "g" : 1000,
       "kg" : 1000000,
       # imperial measurements
       "oz" : 28349.5231,
       "lb" : 453592.37,
       "lbs" : 453592.37 }

units = list( vol.keys() + wt.keys() )

commands = { 
             "convert" : ".convert <num> <unit1> to <unit2> # converts between units. "
                         "allowed units => [volume: tsp, tbsp, oz, cups, pt, qt, gal, mL, L] [mass: mg, g, kg, oz, lb]" ,
             "isop" : ".isop <nick> # checks a user for ops or halfops",
             "help" : None,
             "ping" : ".ping # checks if I'm here and responding to commands",
             "record" : ".record -<lines>!<exceptions> <nick> => <recipe> "
                        "# records the last given number of lines said by someone as a recipe, ignoring the list of exceptions. "
                        "Example: .record -20!1,2 someone => omelet << records the last 20 lines 'someone' said except the last two as 'omelet'. "
                        "I currently remember up to 30 lines for each user per channel.",
             "say" : ".say <stuff>" }


class PanChan(Channel):
  """Uses the strings from namreply to set the values for each user in a channel."""
  def __init__(self, namreply=None):
    Channel.__init__(self)
    self.logdict = IRCDict()
    if namreply:
      for nick in namreply.split(' '):
        if nick[0] == '~': self.ownerdict[nick[1:]] = 1
        elif nick[0] == '@': self.operdict[nick[1:]] = 1
        elif nick[0] == '%': self.halfopdict[nick[1:]] = 1
        elif nick[0] == '+': self.voicedict[nick[1:]] = 1
        else: 
          self.userdict[nick] = 1
          self.logdict[nick] = tempfile.NamedTemporaryFile(bufsize=5120)
          continue
        self.userdict[nick[1:]] = 1
        self.logdict[nick[1:]] = tempfile.NamedTemporaryFile(bufsize=5120)

  def add_user(self, nick):
    self.logdict[nick] = tempfile.NamedTemporaryFile(bufsize=5120)
    Channel.add_user(self, nick)

  #NOTE: Make a new change_user method that transfers the logdict when nicks are changed.

  def clear_mode(self, mode, value=None):
    """Passes on 'a' modes since I didn't feel like making an admindict at the time. Later on I might (have to) though."""
    if mode == 'a':
      pass
    else:
      Channel.clear_mode(self, mode, value)
  
  def get_userlog(self, nick):
    if self.logdict.has_key(nick):
      log = self.logdict[nick]
      log.seek(0)
      return log.readlines()
    return None

  def has_privs(self, nick):
    return self.is_oper(nick) or self.is_halfop(nick) or self.is_owner(nick)

  def handle_modes(self, modes, args):
    m = mod.match(modes)
    if m:
      l = list(args)
      s = m.group('mode1')
      if s[0] == '+':
        for each in s[1:]:
          if each in 'qaohv' and l != []:
            self.set_mode(each, l.pop(0))
          else:
            self.set_mode(each)
      else:
        for each in s[1:]:
          if each in 'qaohv' and l != []:
            self.clear_mode(each, l.pop(0))
          else:
            self.clear_mode(each)
      if m.group('mode2'):
        self.handle_modes(m.group('mode2'), *l)

  def log_user(self, nick, msg):
    log = self.logdict[nick]
    log.seek(0)
    lines = log.readlines()
    if len(lines) < 30: # maybe add a LINE_LIMIT constant somewhere for this later on?
      log.write(msg + '\n')
    elif len(lines) == 30:
      lines.pop(0)
      lines.append(msg + '\n')
      log.seek(0)
      log.writelines(*lines)
    log.flush()

  def set_mode(self, mode, value=None):
    if mode == 'a':
      pass
    else:
      Channel.set_mode(self, mode, value)


class PanHandler(DefaultCommandHandler):
  """Yes, I had a bit of fun with the names."""
  def addressed(self, params, who, chan):
    """Called whenever the bot is addressed by name. Unless I make something cleaner, this is going to be long and regex-y, so bear with me."""
    params = params.strip()
    pantry = self.client.db.pantry
    isop = self.client.channels[chan].has_privs(who)
    query = pantry.find_one({ 'user': who }, {'items': 1})
#    target = who
    m = intro.match(params)
    if m: return "Hi, I'm Marzipan, or Pan for short. It's nice to meet you. :)"
    m = rem.match(params)
    if m: return self.client.quote(m.group('target'), chan, m.group('phrase'))
    m = make.match(params)
    if m:
      if isop:
        pantry.insert({ 'user' : m.group('user'), 'items': [] })
        return "Okay, {}. I made a pantry for {}.".format(who, m.group('user'))
      else:
        return "Sorry, %s, but I can't let you do that." % who
    m = ls.match(params)
    """Note to self: try to clean up all this Mongo query crap. It could probably be better."""
    if m:
      if query:
        items = query['items']
        if items == []:
          return "{}: Your pantry is currently empty. I can add items to it if you like. :)".format(who)
        if len(items) == 1:
          return "{}: Your pantry only contains {} right now.".format(who, items[0])
        if len(items) == 2:
          return "{}: Your pantry only contains {}.".format(who, " and ".join(items))
        return "{}: Your pantry contains: {} and {}.".format(who, ', '.join(items[:-1]), items[-1])
      else:
        """Note to self: make a function that returns pantry errors like the one below. No reason for there to be five of these."""
        return "Sorry, {}, but I don't think you have a pantry...".format(who)
    m = add.match(params)
    if m:
      if query:
        items = query['items']
        items.extend([each.strip() for each in m.group('items').split(',')])
        pantry.update({ 'user': who }, { '$set': {'items': items} })
        return "Okay, {}, I added them to your pantry.".format(who)
      else:
        return "Sorry {}, but I don't think you have a pantry.. :o".format(who) # this line keeps showing up. should probably put it in a variable or something
    m = rm.match(params)
    if m:
      if query:
        items = query['items']
        items = [ item for item in items if item not in [removed.strip() for removed in m.group('items').split(',')] ]
        pantry.update({ 'user': who }, { '$set': {'items': items} })
        return "Okay, {}, I removed them from your pantry.".format(who)
      else:
        return "Sorry, {}, but I don't think you have a pantry...".format(who)
    m = cls.match(params)
    if m:
      if query:
        pantry.update({ 'user': who }, { "$set": {"items": []} })
        return "Okay, {}, I cleared out your pantry.".format(who)
      else:
        return "Sorry {}, but I don't think you have a pantry... >.>".format(who)
    return ""

  def endofmotd(self, server, me, msg):
    """Used instead of a connect callback since it invokes too early."""
    self.client.send("MODE %s +B" % me)
    helpers.nick(self.client, "marzipan")
    helpers.join(self.client, "#playground")
    if self.client.logchan:
      helpers.join(self.client, self.client.logchan)

  def getCommand(self, msg, chan, who):
    m = cmd.match(msg)
    if not m:
      m = addr.match(msg)
      if not m:
        m = http.search(msg)
        if m:
          return HTMLParser().unescape(self.client.crawl(m.group('url')))
        else:
          return ""
      else:
        return self.addressed(m.group('args'), who, chan)
    try:
      f = getattr(self.client, m.group('command'))
    except AttributeError:
      return ""
    else:
      return f(m.group('args'), chan)

  def join(self, who, chan):
    if self.client.channels.has_key(chan):
      who = who.split('!')[0]
      self.client.channels[chan].add_user(who)

  def kick(self, source, chan, target, msg):
    source = source.split('!')[0]
    if source != self.client.nick:
      self.client.channels[chan].remove_user(target)
    else:
      del self.client.channels[chan]

  def mode(self, source, chan, modes, *args):
    if chan[0] == '#':
      """If not a real channel then modes are set by/for the server. We can ignore those for now."""
      self.client.channels[chan].handle_modes(modes, args)

  def namreply(self, server, me, chantype, chan, nicks):
    self.client.channels[chan] = PanChan(nicks)

  def nick(self, before, after):
    if before == self.client.nick or after == self.client.nick:
      """Her name won't change."""
      pass
    for chan in self.client.channels:
      chan.change_nick(before.split('!')[0], after)
    
  def part(self, source, chan, msg=None):
    self.client.channels[chan].remove_user(source.split('!')[0])

  def privmsg(self, nick, chan, msg):
    response = self.getCommand(msg, chan, nick.split('!')[0])
    if chan[0] != "#":
      chan = nick.split('!')[0]
      print "PM | <{}> {}".format(nick, msg)
      if msg == "\x01VERSION\x01":
        """There's no basic ctcp function for some reason, so I have to deal with it here. I'll probably have to make one later."""
        self.client.send("NOTICE %s \x01VERSION Marzipan, the IRC Cooking Bot ver 0.2.9alpha\x01" % chan)
    else:
      print "{} | <{}> {}".format(chan, nick.split('!')[0], msg)
      self.client.channels[chan].log_user(nick.split('!')[0], msg)
    if response:
      helpers.msg(self.client, chan, response)

  def quit(self, source, msg):
    for chan in self.client.channels:
      chan.remove_user(source.split('!')[0])


class Marzipan(IRCClient):
  """Built on the interface a bit so you can add commands by defining methods here and using regex."""
  def __init__(self, logchannel=None, **kwargs):
    """Uses PanHandler automatically."""
    IRCClient.__init__(self, PanHandler, **kwargs)
    print "Successfully created", self
    self.channels = IRCDict()
    self.db = MongoClient(document_class=SON).mzpn
    self.logchan = logchannel

  def convert(self, msg, *args):
    """Converts between units of volume and weight, respectively."""
    m = conv.match(msg.lower())
    if not m:
      return "Invalid conversion command."
    if m.group('unit1') in units and m.group('unit2') in units:
      if m.group('unit1') in vol and m.group('unit2') in vol:
        d = vol
      else:
        d = wt
      try:
        num = float(m.group('num'))
      except ValueError:
        return "... That's not a number :<"
      else:
        return "{:.7} {}".format( num * d[m.group('unit1')] / d[m.group('unit2')], m.group('unit2'))

  def crawl(self, link, *args):
    """Crawls webpages for information and returns it in a certain format."""
    m = crawl.search(Curl().get(link).strip('\n'))
    return "" if not m else m.group('title').strip() + ' | ' + link.split('/')[0]

  def help(self, cmd, *args):
    if not commands.has_key(cmd) or commands[cmd] == None:
      keys = commands.keys()
      return "My current allowed commands are: {} and {}. Use .help <cmd> for more info on each.".format(', '.join(keys[:-1]), keys[-1])
    return commands[cmd]

  def isop(self, nick, chan):
    check = self.channels[chan]
    if check.has_privs(nick):
      return nick + " is an op"
    else:
      if check.has_user(nick):
        return nick + " isn't an op"
      return nick + " isn't a user here"

  def ping(self, *args):
    return "pong"

  def record(self, params, chan):
    """Log recording function for remembering long recipes."""
    m = recd.match(params)
    if m:
      log = self.channels[chan].get_userlog(m.group('target'))
      log.reverse()
      out = []
      if m.group('except'):
        for x in range(0, int(m.group('lines'))):
          if str(x+1) in m.group('except').split(','):
            continue
          out.append(log[x])
      else:
        out = log[0:int(m.group('lines'))]
      oid = self.db.recipes.insert({'name': m.group('recipe'), 'desc': out, 'whose': m.group('target')})
      self.report('record', oid, "Recipe Name: {}, # of lines: {}".format(m.group('recipe'), len(out)))
      return "Okay. Recipe successfully recorded."
    return "Invalid parameters for recipe recorder."

  def report(self, func, oid=None, info="", *args):
    if self.logchan == None:
      return
    elif func == 'record':
      helpers.msg(self, self.logchan, "Inserted new recipe recording [{}] with OID('{}')".format( info, oid) )
    elif func == 'quote':
      helpers.msg(self, self.logchan, "Inserted new quote of {} with OID('{}')".format( info, oid) )
    elif info != "":
      helpers.msg(self, self.logchan, info)


  def say(self, msg, *args):
    return msg

  def search(self, msg, *args):
    """More complex search spider for specific sites. Will add later."""
    pass

  def quote(self, target, chan, phrase, *args):
    """Simple quoting function."""
    log = self.channels[chan].get_userlog(target)
    if self.channels[chan].has_user(target) and log != []:
      for line in log[::-1]:
        if phrase in line:
          oid = self.db.quotes.insert({'user': target, 'quote': line, 'created': datetime.now()})
          self.report('quote', oid)
          return 'Okay. Remembered that {} said, "{}".\n'.format(target, line.strip())
    return "S-sorry, but I don't know what {} said about '{}'".format(target, phrase)

if __name__ == "__main__":
  logging.basicConfig(level=logging.DEBUG)

  cli = Marzipan(logchannel="#marzipan", host="irc.foonetic.net", port=6667, nick="pan", real_name="Marzipan, ran by rtmiu (testing)")
  conn = cli.connect()

  while True:
    try:
      conn.next()
    except KeyboardInterrupt:
      cli.send("QUIT T-there's smoke coming out of the oven...!")

