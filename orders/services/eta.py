# imports from std lib
from datetime import datetime, timedelta
from typing import List, DefaultDict

# imports from project
from orders.models import Order
from orders.services.state import orders_by_id, driver_id_by_order_id, drivers_by_id
from orders.services.engine import get_driver_city_queues
from orders.services.time import round_time, format_time
from orders.constants import _DELIVERY_TIME, get_travel_time

# returns two times creating an eta window of when the order will arrive
def get_eta(order_id: str):
    # extract info about order
    order: Order = orders_by_id[order_id]
    driver_id = driver_id_by_order_id[order_id]
    driver_location: str = drivers_by_id[driver_id].get_current_city()
    driver_queue_by_city: DefaultDict[str, List[int]] = get_driver_city_queues(driver_id)

    # get sorted list of cities driver needs to travel to
    city_travel_time = []
    for iter_city in driver_queue_by_city:
        city_travel_time.append({'city': iter_city, 'time': get_travel_time(driver_location, iter_city)})
    city_travel_time.sort(key=lambda cities: cities['time'])

    # create one master list of orders sorted by city and within city
    orders_in_order: List[str] = []
    for entry in city_travel_time:
        city = entry['city']
        orders_in_order.extend(driver_queue_by_city[city])

    # core logic for time to order delivery
    order_created_time: datetime = order.get_created_at()
    minutes_to_order = get_eta_minutes(driver_location, orders_in_order, order_id)
    raw_eta: datetime = order_created_time + timedelta(minutes=minutes_to_order)

    # clamp: minimum 30 mins out, maximum 2 hours out from order creation, then round up to nearest 15 mins
    clamped_eta = max(order_created_time + timedelta(minutes=30), min(raw_eta, order_created_time + timedelta(hours=2)))
    start_timestamp = round_time(clamped_eta)

    # window: 90 mins if more than 5 active orders, otherwise 60 mins
    window_mins: int = 90 if len(orders_by_id) > 5 else 60
    end_timestamp = start_timestamp + timedelta(minutes=window_mins)

    order.set_etas(start_timestamp, end_timestamp)

    return format_time(start_timestamp), format_time(end_timestamp)

def get_eta_minutes(start_city: str, order_ids_list: List[str], search_id: str) -> int:
    minutes: int = 0

    curr_location: str = start_city
    for order_id in order_ids_list:
        next_location: str = orders_by_id[order_id].get_city()

        minutes_to_add: int = get_travel_time(curr_location, next_location) + _DELIVERY_TIME
        minutes += minutes_to_add

        #buffer_time: int = 0 if len(order_ids_list) < 5 else _BUFFER_TIME
        buffer_time: int = 0
        if order_id == search_id:
            return minutes + buffer_time
        # else
        curr_location: str = next_location

    return minutes