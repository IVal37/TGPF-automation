# imports from project
from orders.constants import HOME_CITY
from orders.services import queries


# A driver's current city is where they last said they were. A city set today is
# still true after a restart, so it is kept; one from an earlier day is stale
# (the driver starts a new day in Davis), as is one that was never set.
def reset_stale_driver_locations() -> None:
    today = queries.today()
    for driver in queries.active_drivers():
        if driver.location_updated_at is None or driver.location_updated_at.date() < today:
            driver.set_current_city(HOME_CITY)
