import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    conninfo=os.environ["DATABASE_URL"],
    min_size=4,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)