#!/usr/bin/env python
import os
import sqlite3
import argparse
import subprocess
import codecs
import re
import urllib
import json
from itertools import permutations, groupby

import sparql

VIEW_FILENAME = os.path.dirname(os.path.realpath(__file__)) + '/views.sql'


def apply_views(conn):
    with open(VIEW_FILENAME) as f:
        f.readline()  # skip first line
        conn.executescript(f.read())


def search_query(from_lang, to_lang, search_term, **kwargs):
    conn = sqlite3.connect(
        'dictionaries/sqlite/prod/%s-%s.sqlite3'
        % (from_lang, to_lang))
    for r in conn.execute("""
                SELECT * FROM (
                    SELECT lexentry, display, display_addition, sense_list,
                           trans_list
                    FROM (
                            SELECT DISTINCT lexentry
                            FROM search_trans
                            WHERE form MATCH ?
                        )
                        JOIN translation USING (lexentry)
                    ORDER BY translation.rowid
                )
                UNION ALL
                SELECT NULL, written_rep, NULL, NULL, trans_list
                FROM search_reverse_trans
                WHERE written_rep MATCH ?
            """, [search_term, search_term]):
        print '%-40s %-20s %-20s %-80s %s' % r


def make_prod_single(lang, **kwargs):
    conn = sqlite3.connect('dictionaries/sqlite/%s.sqlite3' % lang)
    conn.execute(
        "ATTACH DATABASE 'dictionaries/sqlite/prod/%s.sqlite3' AS prod"
        % (lang))

    if lang == 'de':
        conn.executescript("""
            DROP VIEW IF EXISTS lexentry_display;
            CREATE VIEW lexentry_display AS
            WITH noun AS (
                SELECT lexentry, other_written, number
                FROM form
                WHERE pos = 'noun'
                    AND "case" = 'Nominative'
                    AND (inflection = 'WeakInflection'
                         OR inflection IS NULL)
            )
            SELECT lexentry, singular AS display,
                'Pl.: ' || plural AS display_addition
            FROM (
                SELECT lexentry, other_written AS singular
                FROM noun
                WHERE number = 'Singular'
                GROUP BY 1
                HAVING count(DISTINCT other_written) = 1
            ) JOIN (
                SELECT lexentry, other_written AS plural
                FROM noun
                WHERE number = 'Plural'
                GROUP BY 1
                HAVING count(DISTINCT other_written) = 1
            ) USING (lexentry);
        """)
    else:
        conn.executescript("""
            DROP VIEW IF EXISTS lexentry_display;
            CREATE VIEW lexentry_display AS
            SELECT '' AS lexentry, '' AS display, '' AS display_addition
        """)

    print 'Prepare entry'
    conn.executescript("""
        DROP TABLE IF EXISTS prod.entry;
        CREATE TABLE prod.entry AS
        SELECT entry.*, display, display_addition
        FROM entry
             LEFT JOIN lexentry_display USING (lexentry);

        CREATE INDEX prod.entry_written_rep_idx ON entry(written_rep);
    """)

    conn.close()
    print 'Vacuum'
    sqlite3.connect('dictionaries/sqlite/prod/{}.sqlite3'.format(lang)
                    ).execute('VACUUM')


def interactive(from_lang, to_lang, **kwargs):
    with open('/tmp/attach_dbs.sql', 'w') as f:
        f.write(attach_dbs(from_lang, to_lang))
        f.write('\n.read ' + VIEW_FILENAME)
    subprocess.check_call(
        '/usr/local/Cellar/sqlite/3.8.10.2/bin/sqlite3 '
        '-init /tmp/attach_dbs.sql dictionaries/sqlite/%s.sqlite3' % from_lang,
        shell=True)
    #p = subprocess.Popen(['sqlite3', '-interactive'], stdin=subprocess.PIPE)
    #p.communicate(script)


def attach_dbs(from_lang, to_lang):
    return """
        ATTACH DATABASE 'dictionaries/sqlite/{to_lang}.sqlite3' AS other;
        ATTACH DATABASE 'dictionaries/sqlite/{from_lang}-{to_lang}.sqlite3' AS lang_pair;
        ATTACH DATABASE 'dictionaries/sqlite/{to_lang}-{from_lang}.sqlite3' AS other_pair;
        ATTACH DATABASE 'dictionaries/sqlite/prod/{from_lang}-{to_lang}.sqlite3' AS prod;
        ATTACH DATABASE 'dictionaries/sqlite/prod/wikdict.sqlite3' AS wikdict;
    """.format(from_lang=from_lang, to_lang=to_lang)


def make_prod_pair(from_lang, to_lang, **kwargs):
    conn = sqlite3.connect('dictionaries/sqlite/%s.sqlite3' % from_lang)
    conn.executescript(attach_dbs(from_lang, to_lang))
    apply_views(conn)

    print 'Prepare translation'
    conn.executescript("""
        -- required to get a rowid
        CREATE TEMPORARY TABLE grouped_translation_table
        AS SELECT * FROM grouped_translation;

        DROP TABLE IF EXISTS prod.translation;
        CREATE TABLE prod.translation AS
        SELECT lexentry, written_rep, part_of_speech, sense_list,
               min_sense_num, trans_list
        FROM grouped_translation_table
            JOIN (
                SELECT lexentry, part_of_speech
                FROM entry
            ) USING (lexentry)
        ORDER BY grouped_translation_table.rowid
    """)

    print 'Prepare search index'
    conn.executescript("""
        DROP TABLE IF EXISTS prod.search_trans;
        CREATE VIRTUAL TABLE prod.search_trans USING fts4(
            form, lexentry, tokenize=unicode61, notindexed=lexentry
        );
        INSERT INTO prod.search_trans
        SELECT written_rep, lexentry
        FROM prod.translation
        UNION
        SELECT other_written, lexentry
        FROM form
        WHERE lexentry IN (
            SELECT lexentry FROM prod.translation
        )
    """)

    print 'Prepare search index (reversed translation)'
    conn.executescript("""
        DROP TABLE IF EXISTS prod.search_reverse_trans;
        CREATE VIRTUAL TABLE prod.search_reverse_trans USING fts4(
            written_rep, trans_list, tokenize=unicode61, notindexed=trans_list
        );
        INSERT INTO prod.search_reverse_trans
        SELECT written_rep, trans_list
        FROM grouped_reverse_trans
    """)

    conn.execute("""
        DELETE FROM wikdict.lang_pair WHERE from_lang = ? AND to_lang = ?
    """, [from_lang, to_lang])
    conn.execute("""
        INSERT INTO wikdict.lang_pair
        SELECT ?, ?,
            (SELECT count(*) FROM prod.translation),
            (SELECT count(*) FROM prod.search_reverse_trans),
            (SELECT count(*) FROM form)
    """, [from_lang, to_lang])
    print conn.execute("""
        SELECT * FROM wikdict.lang_pair WHERE from_lang = ? AND to_lang = ?
    """, [from_lang, to_lang]).fetchone()
    conn.commit()

    print 'Optimize'
    conn.execute("INSERT INTO prod.search_trans(search_trans) VALUES('optimize');")
    conn.execute("INSERT INTO prod.search_reverse_trans(search_reverse_trans) VALUES('optimize');")
    conn.close()
    print 'Vacuum'
    sqlite3.connect('dictionaries/sqlite/prod/{}-{}.sqlite3'.format(from_lang, to_lang)
                    ).execute('VACUUM')


def make_complete_lang(langs, **kwargs):
    if langs == ['all']:
        langs = sparql.translation_query_type.keys()
    for lang in langs:
        print 'Lang:', lang
        make_form(lang)
        make_entry(lang)
        make_prod_single(lang)


def make_complete_pair(langs, **kwargs):
    if langs == ['all']:
        langs = sparql.translation_query_type.keys()
    assert len(langs) >= 2, 'Need at least two languages'
    #for lang in (from_lang, to_lang):
    #    make_complete_lang([lang])
    print 'Get translations'
    for from_lang, to_lang in permutations(langs, 2):
        print from_lang, to_lang
        sparql.get_translations(from_lang, to_lang)
    print 'Get translations'
    for from_lang, to_lang in permutations(langs, 2):
        print from_lang, to_lang
        make_prod_pair(from_lang, to_lang)


def make_form(lang, **kwargs):
    sparql.get_query('form', sparql.form_query, lang=lang)


def make_entry(lang, **kwargs):
    sparql.get_query('entry', sparql.entry_query, lang=lang)
    # TODO: reject bad rows:
    #   * written_rep IS NULL
    #   * written_rep = ''


def make_importance(langs, **kwargs):
    if langs == ['all']:
        langs = sparql.translation_query_type.keys()
    for lang in langs:
        print 'Lang:', lang
        sparql.get_query('importance', sparql.importance_query, lang=lang)


def make_typeahead_single(lang):
    conn = sqlite3.connect('dictionaries/sqlite/%s.sqlite3' % lang)

    # When searching in two languages, the more popular one will have
    # the higher importance scores for words. To show at least some
    # results from the less poplular language, we normalize the scores
    # for the typeahead
    rows = conn.execute("""
        SELECT lower(substr(x, 1, 3)) AS prefix, x,
            score / (
                SELECT avg(score)
                FROM (
                    SELECT * FROM importance
                    ORDER BY score DESC LIMIT 10000
                )
            )
        FROM (
            SELECT substr(vocable, 5) AS x, score AS score
            FROM importance
            WHERE lower(substr(vocable, 5)) IN (
                SELECT lower(written_rep) FROM entry
            )
        )
        ORDER BY 1, score DESC
    """)

    # make prefix dir for this language
    path = os.path.expanduser('~/tools/typeahead2/%s' % lang)
    try:
        os.mkdir(path)
    except OSError:
        pass

    def save_typeahead(filename, prefix_rows):
        encoded_prefix = urllib.quote_plus(filename)
        filename = path + '/' + encoded_prefix + '.json'
        with codecs.open(filename, 'w', 'utf8') as f:
            words = [r[1:] for r in prefix_rows]
            f.write(json.dumps(words))

    # save words to [prefix].txt
    #singles = []
    for prefix, prefix_rows in groupby(rows, lambda row: row[0]):
        if len(prefix) < 3:
            # print 'Skip short prefix %s' % prefix
            continue
        prefix_rows = list(prefix_rows)
        #if len(prefix_rows) =< 1:
        #    singles += prefix_rows
        #    continue
        save_typeahead(prefix.encode('utf8'), prefix_rows)
    #save_typeahead('_singles', singles)


def make_typeahead(langs, **kwargs):
    if langs == ['all']:
        langs = sparql.translation_query_type.keys()
    for lang in langs:
        print 'Lang:', lang
        make_typeahead_single(lang)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    form = subparsers.add_parser(
        'form', help='lemon:otherForm entries for LexicalEntries')
    form.add_argument('lang')
    form.set_defaults(func=make_form)

    entry = subparsers.add_parser('entry', help='lemon:LexicalEntry entries')
    entry.add_argument('lang')
    entry.set_defaults(func=make_entry)

    translation = subparsers.add_parser('translation')
    translation.add_argument('from_lang')
    translation.add_argument('to_lang')
    translation.set_defaults(func=sparql.get_translations)

    search = subparsers.add_parser('search')
    search.add_argument('from_lang')
    search.add_argument('to_lang')
    search.add_argument('search_term')
    search.set_defaults(func=search_query)

    prod_pair = subparsers.add_parser('prod_pair')
    prod_pair.add_argument('from_lang')
    prod_pair.add_argument('to_lang')
    prod_pair.set_defaults(func=make_prod_pair)

    prod_single = subparsers.add_parser('prod')
    prod_single.add_argument('lang')
    prod_single.set_defaults(func=make_prod_single)

    complete_lang = subparsers.add_parser('complete_lang')
    complete_lang.add_argument('langs', nargs='+', metavar='lang')
    complete_lang.set_defaults(func=make_complete_lang)

    complete_pair = subparsers.add_parser('complete_pair')
    complete_pair.add_argument('langs', nargs='+', metavar='lang')
    complete_pair.set_defaults(func=make_complete_pair)

    inter = subparsers.add_parser('interactive')
    inter.add_argument('from_lang')
    inter.add_argument('to_lang')
    inter.set_defaults(func=interactive)

    importance = subparsers.add_parser('importance')
    importance.add_argument('langs', nargs='+', metavar='lang')
    importance.set_defaults(func=make_importance)

    typeahead = subparsers.add_parser('typeahead')
    typeahead.add_argument('langs', nargs='+', metavar='lang')
    typeahead.set_defaults(func=make_typeahead)

    args = parser.parse_args()
    args.func(**vars(args))