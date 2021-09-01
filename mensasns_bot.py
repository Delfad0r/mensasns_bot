from seleniumrequests import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
import urllib
import datetime
from collections import Counter, OrderedDict

import telegram
import telegram.ext

import logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
import urllib3
urllib3.disable_warnings()

import getpass

class MyDriver(Chrome):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_page_load_timeout(10)
        self.base_url = 'https://spazi.sns.it'
        self.SID = {'lunch' : '31', 'dinner' : '32'}
        self.RID = {'lunch' : ['1278', '1279'], 'dinner' : ['1281', '1280']}
    def login(self, email, password):
        self.get(self.base_url)
        data = {'email' : email, 'password' : password, 'login' : 'submit'}
        self.request('POST', f'{self.base_url}/index.php', data = data, verify = False, timeout = 10)
    def get_resource_url(self, which, date, line = None):
        res = f'{self.base_url}/schedule.php?sid={self.SID[which]}&sd={date.isoformat()}'
        if line is not None:
            res += f'&rid={self.RID[which][line - 1]}'
        return res
    def get_reserve_url(self, which, line, begin, end):
        format_time = lambda t: t.strftime('%Y-%m-%d %H:%M:%S')
        data = {
            'sid' : self.SID[which],
            'rid' : self.RID[which][line - 1],
            'rd' : begin.date().isoformat(),
            'sd' : format_time(begin),
            'ed' : format_time(end)
        }
        return 'https://spazi.sns.it/reservation.php?' + urllib.parse.urlencode(data)
    def get_schedule_data(self, which, date):
        self.get(self.get_resource_url(which, date))
        res = []
        for rid in self.RID[which]:
            print(f'Fetching data for {date} {which}')
            WebDriverWait(self, 20).until(EC.visibility_of_element_located((By.CSS_SELECTOR, f'.reservations[data-resourceid="{rid}"]')))
            selector = f'.reservations[data-resourceid="{rid}"] > div[class^="reserved"] > span'
            spans = self.find_elements_by_css_selector(selector)
            res.append(list(Counter(s.text for s in spans).items()))
        return res
    def logout(self):
        self.get(f'{self.base_url}/logout.php')
        self.delete_all_cookies()
        
def get_progress_bar(perc, width):
    blocks = ['░', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']
    perc *= width
    bar = ''
    for i in range(width):
        j = round(max(0, min(1, perc)) * 8)
        bar += blocks[j]
        perc -= 1
    bar += blocks[1]
    if bar[0] == blocks[0]:
        bar = blocks[1] + bar[1 :]
    return bar

def make_monospace_digits(s):
    return ''.join(chr(ord('𝟶') + int(c)) if c.isdigit() else c for c in s)

class MyBot:
    def __init__(self, token, channels, email, password):
        self.updater = telegram.ext.Updater(token, use_context = True)
        self.bot = self.updater.bot
        self.bot.get_me()
        self.channels = channels
        self.email = email
        self.password = password
        self.active_messages = { c : {} for c in channels}
        driver_options = Options()
        driver_options.headless = True
        self.driver = MyDriver(options = driver_options)
        self.MEALS = {'lunch' : 'Lunch', 'dinner' : 'Dinner'}
        self.SLOTS = {('lunch', 1) : 45, ('lunch', 2) : 35, ('dinner', 1) : 45, ('dinner', 2) : 35}
        self.TURN = datetime.timedelta(minutes = 15)
    def __del__(self):
        for d in self.active_messages.values():
            for m in d.values():
                m.delete()
        self.updater.stop()
        self.driver.quit()
    def run(self):
        self.updater.job_queue.run_repeating(lambda c: self.send_updates(), 60, first = 1)
        self.updater.start_polling()
        self.updater.idle()
    def get_meal_time(self, which, date, line):
        if date.weekday() in [5, 6]:
            if line == 2:
                return None, None
            if which == 'lunch':
                b, e = '12:30', '13:45'
            else:
                b, e = '19:30', '20:30'
        else:
            if line == 2:
                if which == 'lunch':
                    b, e = '12:45', '13:45'
                else:
                    return None, None
            elif which == 'lunch':
                b, e = '12:30', '14:15'
            else:
                b, e = '19:30', '20:45'
        f = lambda t: datetime.datetime.combine(date, datetime.time.fromisoformat(t))
        return f(b), f(e)
    def send_updates(self):
        relevant_meals = []
        now = datetime.datetime.now()
        for day_offset in [0, 1]:
            date = datetime.date.today() + datetime.timedelta(days = day_offset)
            for which in ['lunch', 'dinner']:
                b, e = self.get_meal_time(which, date, 1)
                if(e > now and b < now + datetime.timedelta(days = 1)):
                    relevant_meals.append((date, which))
        relevant_meals = relevant_meals[: 2]
        for d in self.active_messages.values():
            for k in list(d):
                if k not in relevant_meals:
                    d[k].delete()
                    del d[k]
        for d, w in relevant_meals:
            text = self.get_message_text(d, w)
            for c in self.channels:
                if (d, w) in self.active_messages[c]:
                    try:
                        self.active_messages[c][(d, w)].edit_text(text[c], parse_mode = 'MarkdownV2')
                    except telegram.error.BadRequest:
                        pass
                else:
                    self.active_messages[c][(d, w)] = self.bot.send_message(self.channels[c], text[c], parse_mode = 'MarkdownV2', disable_notification = True)
    def get_message_text(self, date, which):
        self.driver.login(self.email, self.password)
        data = self.driver.get_schedule_data(which, date)
        res = { 'normal' : [], 'apple' : [], 'narrow' : [] }
        for l, d in zip([1, 2], data):
            b, e = self.get_meal_time(which, date, l)
            if b is None:
                continue
            url = self.driver.get_resource_url(which, date, l)
            header = f'*[{date.strftime("%A %d/%m")} \\- {self.MEALS[which]}, line {l}]({url})*'
            res['normal'].append(header)
            res['apple'].append(header)
            res['narrow'].append(header)
            slots = OrderedDict()
            t = b
            while t < e:
                slots[t.time()] = 0;
                t += self.TURN
            for f, n in d:
                t = datetime.datetime.strptime(f.split('-')[0], '%I:%M %p').time()
                assert t in slots, '%s %s' % (str(t), str(slots))
                slots[t] = n
            slot_strings = []
            format_time = lambda t: t.strftime('%H:%M')
            for c in res:
                for t, n in slots.items():
                    begin_t = datetime.datetime.combine(date, t)
                    end_t = begin_t + self.TURN
                    if n >= self.SLOTS[(which, l)]:
                        symbol = '⛔️'
                    elif n >= self.SLOTS[(which, l)] - 5:
                        symbol = '⚠️'
                    else:
                        symbol = '🟢'
                    time_str = f'{format_time(begin_t)}\\-{format_time(end_t)}'
                    perc_str = f'{n:2}/{self.SLOTS[(which, l)]}'
                    if c in ['apple', 'narrow']:
                        time_str = make_monospace_digits(time_str)
                        perc_str = make_monospace_digits(perc_str)
                    if end_t > datetime.datetime.now():
                        url = self.driver.get_reserve_url(which, l, begin_t, end_t)
                        s = f'*[{time_str}]({url})*'
                    else:
                        s = f'*{time_str}*'
                        symbol = '➖'
                    if c in ['normal', 'apple']:
                        width = 8
                    elif c == 'narrow':
                        width = 5
                    res[c].append(f'{s} `{get_progress_bar(n / self.SLOTS[(which, l)], width)}{symbol}` `{perc_str}`')
                res[c].append('')
        return { c : '\n'.join(s) for c, s in res.items() }

email = input('SNS email: ')
password = getpass.getpass()
channels = { 'normal' : '@mensasnsupdates', 'apple' : '@mensasnsupdatesapple', 'narrow' : '@mensasnsupdatesnarrow' }
token = open('token.txt', 'r').read().strip()

bot = MyBot(token, channels, email, password)
try:
    bot.run()
finally:
    bot.driver.close()
    del bot
