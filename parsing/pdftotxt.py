from argparse import ArgumentParser
from glob import glob
import subprocess
import re
import dateparser
import csv
from iso4217 import Currency
from itertools import tee
from os import path
import logging


DATE_CLUES = ['statement date', 'as at']
CURRENCIES = set([c.code for c in Currency])
CURRENCY_AMOUNT_REGEX = '({} \d+[\.|,|\d]*\d+)'
LANGUAGES = ['en']
INCORRECT_PWD = 'Incorrect password'


def parse_statement_date(line, iterator):
    line_lower = line.lower()
    for clue in DATE_CLUES:
        if clue in line_lower:
            statement_date = parse_date(line_lower.split(clue)[-1])
            if statement_date:
                return statement_date
            else: # in OCBC & ANZ's case, need to look at the next non-empty line
                line = next(iterator).strip()
                while not line:
                    line = next(iterator).strip()
                groups = re.split(r'\s{2,}', line)
                if not groups:
                    return
                statement_date = parse_date(groups[0])
                if statement_date:
                    return statement_date


# "(1,380.77)" and "1,380.77 CR" will translate to -1380.77
def parse_amount(amount_str):
    try:
        amount_str = amount_str.lower().strip().replace(',', '')
        cleaned = re.sub(r'\(|\)|cr', '', amount_str).strip()
        amount = float(cleaned)
        if cleaned != amount_str:
            amount = -amount
        return amount
    except ValueError:
        logging.error(f'Failed to parse amount string {amount_str}')
        return None


# cross year statement needs statement_date to determine the year of date_str
# because transaction date_str often don't contain year
def parse_transaction_date(date_str, statement_date):
    transaction_date = parse_date(date_str).replace(year=statement_date.year)
    alt_date = transaction_date.replace(year=(statement_date.year-1))
    if (abs(alt_date - statement_date) < abs(transaction_date - statement_date)):
        transaction_date = alt_date
    return format_date(transaction_date)


def format_date(datetime_obj):
    return datetime_obj.strftime('%Y-%m-%d')


def parse_date(date_str):
    return dateparser.parse(date_str, locales=['en-SG'])

# Foreign currency transaction often include the foreign currency &
# amount in a separate line
def peek_forward_for_currency(iterator, max_lines=2):
    for i in range(0, max_lines): # look no further than 2 lines
        line = next(iterator).strip()
        if line and len(line) > 3:
            for currency in CURRENCIES:
                found = re.search(CURRENCY_AMOUNT_REGEX.format(currency),
                                  line, re.IGNORECASE)
                if found:
                    return found.group(1)


def process_pdf(filename, csv_writer, pdftotxt_bin='pdftotext',
                include_source=True, password=None, **kwargs):

    # recursive fn
    def process_line(iterator, statement_date):
        try:
            # skip empty & short lines
            line, groups = None, None
            while not line or not groups or len(groups) < 3 or len(groups[0]) < 5:
                line = next(iterator).strip()
                if line:
                    groups = re.split(r'\s{2,}', line)
                    if not statement_date:
                        statement_date = parse_statement_date(line, iterator)

            # consider a line as a transaction when it begins with date
            date_found = parse_date(groups[0])
            if date_found:
                description_end_index = -1
                if '$' in groups:
                    # everything between date & $ is considered description
                    description_end_index = groups.index('$')
                description = ' '.join(groups[1:description_end_index])

                # make a copy for peeking
                iterator, iterator_copy = tee(iterator)
                foreign_amount = peek_forward_for_currency(iterator_copy)

                date = parse_transaction_date(groups[0], statement_date)
                amount = parse_amount(groups[-1])
                row = [date, description, amount, foreign_amount,
                       format_date(statement_date)]
                if include_source:
                    row.append(path.basename(filename))
                csv_writer.writerow(row)

            process_line(iterator, statement_date)
        except StopIteration:
            pass

    print(filename)
    if password:
        command = [pdftotxt_bin, '-layout', '-upw', password, filename, '-']
    else:
        command = [pdftotxt_bin, '-layout', filename, '-']
    try:
        result = subprocess.check_output(command, stderr=subprocess.PIPE, **kwargs)
        lines = result.decode('utf-8').split('\n')
        statement_date = None
        process_line(iter(lines), statement_date)
    except subprocess.CalledProcessError as grepexc:
        err = grepexc.stderr.decode('utf-8')
        if INCORRECT_PWD in err:
            raise RuntimeError(INCORRECT_PWD)
        print("error code", grepexc.returncode, err)


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("input", help="Input pdf file or directory")
    parser.add_argument("-o", "--output", dest="output",
                        help="output csv filename", default="out.csv")
    args = parser.parse_args()

    with open(args.output, 'w', encoding='utf-8') as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow(['date', 'description', 'amount', 'foreign_amount',
                             'statement_date', 'source'])
        if path.isdir(args.input):
            for filename in glob('{}/*.pdf'.format(args.input)):
                process_pdf(filename, csv_writer)
        else:
            process_pdf(args.input, csv_writer)
