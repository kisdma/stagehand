from __future__ import absolute_import
import os
import time
import re
import logging
import random
import gc
from datetime import datetime, timedelta
import kaa, kaa.rpc, kaa.config

from . import web
from .tvdb import TVDB
from .config import config
from .utils import fixsep
from .searchers import search, SearcherError
from .retrievers import retrieve, RetrieverError
from .notifiers import notify, NotifierError

log = logging.getLogger('stagehand.manager')


class Manager(object):
    def __init__(self, cfgdir=None, datadir=None, cachedir=None):
        self.series_by_name = {}
        self._retrieve_queue = []
        # The series/episodes the _process_retrieve_queue() is actively
        # processing now. 
        self._retrieve_queue_current = None
        self._check_new_timer = kaa.AtTimer(self.check_new_episodes)

        if not datadir:
            # No defaults allowed right now.
            raise ValueError('No data directory given')
        if not cachedir:
            cachedir = os.path.join(os.getenv('XDG_CACHE_HOME', '~/.cache'), 'stagehand')
        if not cfgdir:
            cfgdir = os.path.join(os.getenv('XDG_CONFIG_HOME', '~/.config'), 'stagehand')

        # static web handler does path checking of requests, so make sure
        # datadir is an absolute, not relative path.
        self.datadir = os.path.abspath(os.path.expanduser(datadir))
        self.cachedir = os.path.expanduser(cachedir)
        self.cfgdir = os.path.expanduser(cfgdir)
        self.cfgfile = os.path.join(self.cfgdir, 'config')

        if not os.path.isdir(self.datadir):
            raise ValueError("Data directory %s doesn't exist" % self.datadir)
        if not os.path.exists(self.cfgdir):
            os.makedirs(self.cfgdir)
            config.save(self.cfgfile)
        else:
            config.load(self.cfgfile)
        # Monitor config file for changes.
        config.watch()
        config.autosave = True
        config.signals['reloaded'].connect(self._config_reloaded)

        if not os.path.exists(os.path.join(self.cachedir, 'web')):
            os.makedirs(os.path.join(self.cachedir, 'web'))
        if not os.path.exists(os.path.join(self.cachedir, 'logs')):
            os.makedirs(os.path.join(self.cachedir, 'logs'))

        handler = logging.FileHandler(os.path.join(self.cachedir, 'logs', 'stagehand.log'))
        handler.setFormatter(logging.getLogger().handlers[0].formatter)
        logging.getLogger().addHandler(handler)

        handler = logging.FileHandler(os.path.join(self.cachedir, 'logs', 'http.log'))
        handler.setFormatter(logging.getLogger('stagehand.http').handlers[0].formatter)
        logging.getLogger('stagehand.http').addHandler(handler)

        self.tvdb = TVDB(os.path.join(self.cfgdir, 'tv.db'))
        self.rpc = kaa.rpc.Server('stagehand')
        self.rpc.register(self)


    @property
    def series(self):
        import traceback
        traceback.print_stack()
        return self.tvdb.series


    def _config_reloaded(self, changed):
        log.info('config file changed; reloading')
        self._load_series_from_config()


    @kaa.rpc.expose()
    def shutdown(self):
        kaa.main.stop()

    @kaa.rpc.expose()
    def pid(self):
        return os.getpid()


    def _build_series_by_name_dict(self):
        # XXX: what about different series with the same name?
        self.series_by_name = dict((s.name_as_url_segment, s) for s in self.tvdb.series)


    @kaa.coroutine()
    def _check_update_tvdb(self):
        servertime = self.tvdb.get_last_updated()
        if servertime and time.time() - float(servertime) > 60*60*12:
            count = yield self.tvdb.sync()
            # FIXME: if count, need to go through all episodes and mark future episodes as STATUS_NEED


    @kaa.coroutine()
    def _add_series_to_db(self, id, fast=False):
        log.info('adding new series %s to database', id)
        series = yield self.tvdb.add_series_by_id(id, fast=fast)
        if not series:
            log.error('provider did not know about %s', id)
            yield None
        log.debug('found series %s (%s) on server', id, series.name)

        # Initialize status for all old episodes as STATUS_IGNORE.  Note that
        # episodes with no airdate can be future episodes, so we mustn't
        # set those to ignore.
        # XXX: changed weeks
        cutoff = datetime.now() - timedelta(weeks=0)
        for ep in series.episodes:
            if ep.airdate and ep.airdate < cutoff:
                ep.status = ep.STATUS_IGNORE

        yield series


    @kaa.coroutine()
    def add_series(self, id):
        """
        Add new series by id, or return existing series if already added.
        """
        series = self.tvdb.get_series_by_id(id)
        if not series:
            series = yield self._add_series_to_db(id, fast=True)
            if not self.tvdb.get_config_for_series(id, series):
                config.series.append(config.series(id=id, path=fixsep(series.name)))
            self._build_series_by_name_dict()
        yield series


    def delete_series(self, id):
        series = self.tvdb.get_series_by_id(id)
        if not series:
            return
        # Delete from config before we delete from database, since accessing
        # series.cfg indirectly needs the dbrow.
        try:
            config.series.remove(series.cfg)
        except ValueError:
            pass
        self.tvdb.delete_series(series)
        self._build_series_by_name_dict()


    @kaa.coroutine()
    def _load_series_from_config(self):
        """
        Ensure all the TV series in the config are included in the DB.
        """
        seen = set()
        for cfg in config.series:
            try:
                series = self.tvdb.get_series_by_id(cfg.id)
            except ValueError, e:
                log.error('malformed config: %s', e)
                continue

            if not series:
                log.info('discovered new series %s in config; adding to database.', cfg.id)
                try:
                    series = yield self._add_series_to_db(cfg.id)
                except Exception, e:
                    log.exception('failed to add series %s', cfg.id)

                if not series:
                    # Could not be added to DB, probably because it doesn't exist.
                    # _add_series_to_db() will log an error about it.
                    continue

            if cfg.path == kaa.config.get_default(cfg.path):
                # Set the path based on the show name explicitly to make the
                # config file more readable.
                cfg.path = fixsep(series.name)

            if cfg.provider != series.provider.NAME:
                if not cfg.provider:
                    cfg.provider = kaa.config.get_default(cfg.provider)
                try:
                    yield series.change_provider(cfg.provider)
                except ValueError, e:
                    log.error('invalid config: %s', e.args[0])

            # Add all ids for this series to the seen list.
            seen.update(series.ids)

        self._build_series_by_name_dict()

        # Check the database for series that aren't in the config.  This indicates the
        # DB is out of sync with config.  Log an error, and mark the series as
        # ignored.
        for series in self.tvdb.series:
            if series.id not in seen:
                log.error('series %s (%s) in database but not config; ignoring', series.id, series.name)
                self.tvdb.ignore_series_by_id(series.id)




    def _check_episode_queued_for_retrieval(self, ep):
        """
        Is the given episode currently queued for retrieval?
        """
        if self._retrieve_queue_current and ep in self._retrieve_queue_current[1]:
            return True
        for series, results in self._retrieve_queue:
            if ep in results:
                return True
        return False



    def _shutdown(self):
        log.info('shutting down')
        config.save(self.cfgfile)


    @kaa.coroutine()
    def start(self):
        # TODO: randomize time, twice a day
        kaa.Timer(self._check_update_tvdb).start(60*60, now=True)
        kaa.signals['shutdown'].connect_once(self._shutdown)
        #web.notify('Global alert', 'Stagehand was restarted')
        yield self._load_series_from_config()
        # TODO: need a "light" check where we don't actually search but resume
        # any aborted downloads
        #yield self.check_new_episodes()
        #kaa.OneShotTimer(self.tvdb._update_series,79349).start(2)

        # TODO: make hours configurable
        check_hours = (4, 11, 16, 21)
        check_min = random.randint(0, 59)
        log.info('scheduling checks at %s', ', '.join('%d:%02d' % (hour, check_min) for hour in check_hours))
        self._check_new_timer.start(hour=check_hours, min=check_min)
        #yield self._check_new_episodes(only=[self.series[73762]])
        #yield notify([])
        #yield self.tvdb._update_series(self.tvdb.providers['thetvdb'], u'75897', dirty=[self.tvdb.providers['thetvdb']])


    @kaa.coroutine(policy=kaa.POLICY_SINGLETON)
    def check_new_episodes(self, only=[], force_next=False):
        log.info('checking for new episodes and availability')
        # Get a list of all episodes that are ready for retrieval, building a list by series.
        need = {}
        for series in self.tvdb.series:
            if only and series not in only:
                continue
            needlist = []
            for ep in series.episodes:
                # TODO: force_next: if True, force-add the first STATUS_NEED/NONE episode
                # for the latest season regardless of airdate.  (Gives the user a way
                # to force an update if airdate is not correct on tvdb.)
                if ep.ready and not ep.series.cfg.paused:
                    log.debug('need %s %s (%s): %s', series.name, ep.code, ep.airdatetime.strftime('%Y-%m-%d %H:%M'), ep.name)
                    if self._check_episode_queued_for_retrieval(ep):
                        log.debug('episode is already queued for retrieval, skipping')
                        log.debug('retrieve queue current=%s, others=%s', self._retrieve_queue_current, self._retrieve_queue)
                    else:
                        needlist.append(ep)
            if needlist:
                need[series] = needlist

        # XXX find a better place for this
        gc.collect()
        if gc.garbage:
            log.warning('uncollectable garbage exists: %s', gc.garbage)

        found = []
        if not need:
            log.info('no new episodes; we are all up to date')
        elif not config.searchers.enabled:
            log.error('episodes require fetching but no searchers are enabled')
        else:
            found = yield self._search_and_retrieve_needed_episodes(need)
        yield need, found


    @kaa.coroutine()
    def _search_and_retrieve_needed_episodes(self, need):
        """
        Go through each series' need list and do a search for the required episodes,
        retrieving them if available.
        """
        episodes_found = []
        for series, episodes in need.items():
            earliest = min(ep.airdate for ep in episodes if ep.airdate) or None
            if earliest:
                earliest = (earliest - timedelta(days=3)).strftime('%Y-%m-%d')

            # XXX: should probably review these wild-ass min size guesses
            mb_per_min = 5.5 if series.cfg.quality == 'HD' else 3
            min_size = (series.runtime or 30) * mb_per_min * 1024 * 1024
            # FIXME: magic factor
            ideal_size = min_size * (10 if series.cfg.quality == 'Any' else 5)

            log.info('searching for %d episode(s) of %s', len(episodes), series.name)
            # TODO: ideal_size
            results = yield search(series, episodes, date=earliest, ideal_size=ideal_size,
                                   min_size=min_size, quality=series.cfg.quality)
            if results:
                # We have results, so add them to the retrieve queue and start
                # the retriever coroutine (which is a no-op if it's already
                # running, due to POLICY_SINGLETON).
                #
                # FIXME: need a way to cache results for a given episode, so that if we
                # restart, we have a way to resume downloads without full searching.
                for ep, ep_results in results.items():
                    for r in ep_results:
                        log.debug2('result %s (%dM)', r.filename, r.size / 1048576.0)
                episodes_found.extend(results.keys())
                self._retrieve_queue.append((series, results))
                self._process_retrieve_queue()

        log.debug('new episode check finished, found %d results', len(episodes_found))
        yield episodes_found


    @kaa.coroutine(policy=kaa.POLICY_SINGLETON)
    def _process_retrieve_queue(self):
        retrieved = []
        while self._retrieve_queue:
            # Before popping, sort retrieve queue so that result sets with
            # older episodes appear first.
            series, results = self._retrieve_queue_current = self._retrieve_queue.pop(0)
            # For the given result set, sort according to older episodes
            for ep, ep_results in sorted(results.items(), key=lambda (ep, _): ep.code):
                # Sanity check.
                if ep.status == ep.STATUS_HAVE:
                    log.error('BUG: scheduled to retrieve %s %s but it is already STATUS_HAVE', 
                              ep.series.name, ep.code)
                    continue
                # Check to see if the episode exists locally.
                elif ep.filename and os.path.exists(os.path.join(ep.season.path, ep.filename)):
                    # The episode filename exists.  Do we need to resume?
                    if ep.search_result:
                        # Yes, there is a search result for this episode, so resume it.
                        log.info('resuming download from last search result')
                        success = yield self._get_episode(ep, ep.search_result)
                        if success:
                            retrieved.append(ep)
                            # Move onto the next episode.
                            continue
                        else:
                            log.warning('download failed, trying other search results')
                            ep.filename = ep_search_result = None
                    else:
                        # XXX: should we move it out of the way and try again?
                        log.error('retriever was scheduled to fetch %s but it already exists; aborting', 
                                  ep.filename)
                        continue

                # Find the highest scoring item for this episode and retrieve it.
                # TODO: also validate series name.
                for result in ep_results:
                    success = yield self._get_episode(ep, result)
                    if success:
                        retrieved.append(ep)
                        # Break result list for this episode and move onto next episode.
                        break

        self._retrieve_queue_current = None
        if retrieved:
            yield notify(retrieved)


    @kaa.coroutine()
    def _get_episode(self, ep, search_result):
        if not os.path.isdir(ep.season.path):
            # TODO: handle failure
            os.makedirs(ep.season.path)

        # Determine name of target file based on naming preferences.
        if config.naming.rename:
            ext = os.path.splitext(search_result.filename)[-1]
            target = ep.preferred_path + kaa.py3_b(ext.lower())
        else:
            target = os.path.join(ep.season.path, kaa.py3_b(search_result.filename))

        ep.search_result = search_result
        ep.filename = os.path.basename(target)

        msg = 'starting retrieval of %s %s (%s)' % (ep.series.name, ep.code, search_result.searcher)
        log.info(msg)
        web.notify('Episode Download', msg.capitalize())

        try:
            yield retrieve(search_result, target, ep)
        except RetrieverError, e:
            ep.filename = None
            if os.path.exists(target):
                # TODO: handle permission problem
                #os.unlink(target)
                print('WOULD DELETE', target)
            log.error(e.args[0])
            yield False
        else:
            # TODO: notify per episode (as well as batches)
            log.info('successfully retrieved %s %s', ep.series.name, ep.code)
            ep.status = ep.STATUS_HAVE
            yield True


