from seleniumrequests import Firefox
from selenium.webdriver.firefox.options import Options
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

class MyDriver(Firefox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = 'https://spazi.sns.it'
        self.SID = {'lunch' : '31', 'dinner' : '32'}
        self.RID = {'lunch' : ['1278', '1279'], 'dinner' : ['1281', '1280']}
    def login(self, email, password):
        self.get(self.base_url)
        data = {'email' : email, 'password' : password, 'login' : 'submit'}
        self.request('POST', f'{self.base_url}/index.php', data = data, verify = False)
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
    def get_schedule_data(self, which, date = datetime.date.today()):
        self.get(f'{self.base_url}/schedule.php?sid={self.SID[which]}&sd={date.isoformat()}')
        res = []
        for rid in self.RID[which]:
            WebDriverWait(self, 20).until(EC.visibility_of_element_located((By.CSS_SELECTOR, f'.reservations[data-resourceid="{rid}"]')))
            selector = f'.reservations[data-resourceid="{rid}"] > div[data-resid] > span'
            spans = self.find_elements_by_css_selector(selector)
            res.append(list(Counter(s.text for s in spans).items()))
        return res
    def logout(self):
        self.get(f'{self.base_url}/logout.php')
        self.delete_all_cookies()
        
def get_progress_bar(perc):
    blocks = ['░', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']
    perc *= 8
    bar = ''
    for i in range(8):
        j = round(max(0, min(1, perc)) * 8)
        bar += blocks[j]
        perc -= 1
    bar += blocks[1]
    if bar[0] == blocks[0]:
        bar = blocks[1] + bar[1 :]
    return bar

class MyBot:
    def __init__(self, token, channel, email, password):
        self.updater = telegram.ext.Updater(token, use_context = True)
        self.bot = self.updater.bot
        self.bot.get_me()
        self.channel = channel
        self.email = email
        self.password = password
        self.active_messages = {}
        driver_options = Options()
        driver_options.headless = True
        self.driver = MyDriver(options = driver_options)
        self.MEALS = {'lunch' : 'Lunch', 'dinner' : 'Dinner'}
        self.SLOTS = {1 : 30, 2 : 25}
        self.TURN = datetime.timedelta(minutes = 15)
    def __del__(self):
        for m in self.active_messages.values():
            m.delete()
        self.updater.stop()
        self.driver.quit()
    def run(self):
        self.updater.job_queue.run_repeating(lambda c: self.send_updates(), 60, first = 1)
        self.updater.start_polling()
        self.updater.idle()
    def get_meal_time(self, which, date):
        if date.weekday in [5, 6]:
            if which == 'lunch':
                b, e = '12:30', '13:45'
            else:
                b, e = '19:30', '20:30'
        else:
            if which == 'lunch':
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
                b, e = self.get_meal_time(which, date)
                if(e > now and b < now + datetime.timedelta(days = 1)):
                    relevant_meals.append((date, which))
        relevant_meals = relevant_meals[: 2]
        for k in list(self.active_messages):
            if k not in relevant_meals:
                self.active_messages[k].delete()
                del self.active_messages[k]
        for d, w in relevant_meals:
            text = self.get_message_text(d, w)
            if (d, w) in self.active_messages:
                try:
                    self.active_messages[(d, w)].edit_text(text, parse_mode = 'MarkdownV2')
                except telegram.error.BadRequest:
                    pass
            else:
                self.active_messages[(d, w)] = self.bot.send_message(self.channel, text, parse_mode = 'MarkdownV2')
    def get_message_text(self, date, which):
        self.driver.login(self.email, self.password)
        data = self.driver.get_schedule_data(which, date)
        res = []
        b, e = self.get_meal_time(which, date)
        for l, d in zip([1, 2], data):
            header = f'*{date.strftime("%A %x")} \\- {self.MEALS[which]}, line {l}*'
            slots = OrderedDict()
            t = b
            while t < e:
                slots[t.time()] = 0;
                t += self.TURN
            for f, n in d:
                t = datetime.datetime.strptime(f.split('-')[0], '%I:%M %p').time()
                assert(t in slots)
                slots[t] = n
            slot_strings = []
            format_time = lambda t: t.strftime('%H:%M')
            for t, n in slots.items():
                begin_t = datetime.datetime.combine(date, t)
                end_t = begin_t + self.TURN
                if end_t > datetime.datetime.now():
                    url = self.driver.get_reserve_url(which, l, begin_t, end_t)
                    s = f'*[{format_time(begin_t)}\\-{format_time(end_t)}]({url})*'
                else:
                    s = f'*{format_time(begin_t)}\\-{format_time(end_t)}*'
                if n == self.SLOTS[l]:
                    symbol = '⛔️'
                elif n >= self.SLOTS[l] - 5:
                    symbol = '⚠️'
                else:
                    symbol = '🟢'
                s += f' `{get_progress_bar(n / self.SLOTS[l])}{symbol}` `{n:2}/{self.SLOTS[l]}`'
                slot_strings.append(s)
            res.append('\n'.join([header] + slot_strings))
        return '\n\n'.join(res)

email = input('SNS email: ')
password = getpass.getpass()
channel = '@mensasnsupdates'
token = open('token.txt', 'r').read().strip()

bot = MyBot(token, channel, email, password)
try:
    bot.run()
except Exception as e:
    del bot
    raise e
