# imports from std lib
from datetime import datetime, timedelta
from typing import List, DefaultDict

# imports from project
from orders.models import Driver, Order
from orders.services import queries
from orders.services.engine import get_driver_city_queues
from orders.services.time import round_time, format_time
from orders.services.hours import clamp_to_opening
from orders.constants import _DELIVERY_TIME, get_travel_time

# a customer who picked a delivery window gets that window when it is no earlier
# than the system's own estimate. Saves it on the order; returns whether it applied.
def apply_scheduled_window(order: Order, start: datetime, end: datetime) -> bool:
    if order.eta_end is None or order.eta_end > end:
        return False
    order.set_etas(start, end)
    return True

# returns two times creating an eta window of when the order will arrive
def get_eta(order_id: str):
    # extract info about order
    order: Order = Order.objects.get(order_id=order_id)
    driver_id = order.driver_id
    driver_location: str = Driver.objects.get(pk=driver_id).get_current_city()
    driver_queue_by_city: DefaultDict[str, List[str]] = get_driver_city_queues(driver_id)
    # one lookup of every stop's city, shared by the route-time calculations below
    cities = {oid: city for city, oids in driver_queue_by_city.items() for oid in oids}

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
    minutes_to_order = get_eta_minutes(driver_location, orders_in_order, order_id, cities)
    raw_eta: datetime = order_created_time + timedelta(minutes=minutes_to_order)

    # clamp: minimum 30 mins out, maximum 2 hours out from order creation
    clamped_eta = max(order_created_time + timedelta(minutes=30), min(raw_eta, order_created_time + timedelta(hours=2)))

    # window: 90 mins if more than 5 real (non-shell) active orders, otherwise 60 mins
    real_order_count = queries.open_real_order_count()
    window_mins: int = 90 if real_order_count > 5 else 60

    # slide the anchor from window-start (1st stop on the route) toward window-middle (later stops)
    # so later customers see a tighter, more accurate window rather than a padded one.
    # the shift is capped by the *actual* travel minutes since the front of the route, so it can
    # never outrun the real gap between stops and invert/collapse two orders' windows
    first_order_minutes = get_eta_minutes(driver_location, orders_in_order, orders_in_order[0], cities) if orders_in_order else 0
    minutes_since_front = max(0, minutes_to_order - first_order_minutes)
    anchor_shift = min(window_mins * 0.5, minutes_since_front)
    start_timestamp = clamp_to_opening(round_time(clamped_eta - timedelta(minutes=anchor_shift)))
    end_timestamp = start_timestamp + timedelta(minutes=window_mins)

    order.set_etas(start_timestamp, end_timestamp)

    return format_time(start_timestamp), format_time(end_timestamp)

# cities: {order_id: city} for the stops in order_ids_list; looked up when not given
def get_eta_minutes(start_city: str, order_ids_list: List[str], search_id: str, cities=None) -> int:
    if cities is None:
        cities = queries.cities_by_order_id(order_ids_list)
    minutes: int = 0

    curr_location: str = start_city
    for order_id in order_ids_list:
        next_location: str = cities[order_id]

        minutes_to_add: int = get_travel_time(curr_location, next_location) + _DELIVERY_TIME
        minutes += minutes_to_add

        #buffer_time: int = 0 if len(order_ids_list) < 5 else _BUFFER_TIME
        buffer_time: int = 0
        if order_id == search_id:
            return minutes + buffer_time
        # else
        curr_location: str = next_location

    return minutes