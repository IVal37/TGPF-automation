# imports from std lib
from datetime import timedelta

# round datetime to the next of some interval of minutes
# @params:
#   dt - datetime object to be rounded
#   minutes - minute interval to be rounded to
#   strict - round up to next interval when exactly on an interval
# @returns:
#   rounded_time - time rounded to next interval
def round_time(dt, minutes=15, *, strict=False):
    sec_since_hour = minutes * 60 
    sec_time = dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
    add = (sec_since_hour - (sec_time % sec_since_hour)) % sec_since_hour
    if strict and add == 0:
        add = sec_since_hour
    rounded_time = (dt + timedelta(seconds=add)).replace(second=0, microsecond=0)
    return rounded_time

# formats datetime for readability
# @params: 
#   dt - datetime object to be formatted
# @returns:
#   dt_str - datetime as string
def format_time(dt):
    try:
        dt_str = dt.strftime("%-I:%M")
    except ValueError:
        dt_str = dt.strftime("%#I:%M")
    
    return dt_str