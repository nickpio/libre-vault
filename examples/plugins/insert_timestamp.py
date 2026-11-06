"""Insert a local timestamp using Libre Vault's normal undoable edit path."""
from datetime import datetime


API_VERSION = 1


def setup(api):
    def insert_timestamp():
        api.replace_selection(datetime.now().astimezone().isoformat(timespec='seconds'))
        api.show_status('Inserted timestamp')

    api.register_command('insert_timestamp', 'Insert timestamp', insert_timestamp)
