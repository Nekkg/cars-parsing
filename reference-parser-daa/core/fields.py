"""Keep PostgreSQL arrays; serialize string lists as JSON on SQLite.

Historical migrations import this field too, so a fresh SQLite database can
replay the same migration graph without deleting or faking migrations.
"""
import json
from django.contrib.postgres.fields import ArrayField as PostgresArrayField


class ArrayField(PostgresArrayField):
    def db_type(self, connection):
        return 'text' if connection.vendor == 'sqlite' else super().db_type(connection)

    def cast_db_type(self, connection):
        return self.db_type(connection) if connection.vendor == 'sqlite' else super().cast_db_type(connection)

    def get_placeholder(self, value, compiler, connection):
        return '%s' if connection.vendor == 'sqlite' else super().get_placeholder(value, compiler, connection)

    def get_db_prep_value(self, value, connection, prepared=False):
        if connection.vendor == 'sqlite':
            return None if value is None else json.dumps(value, ensure_ascii=False)
        return super().get_db_prep_value(value, connection, prepared)

    def from_db_value(self, value, expression, connection):
        if connection.vendor == 'sqlite':
            return json.loads(value) if isinstance(value, str) else value
        if hasattr(self.base_field, 'from_db_value') and value is not None:
            return [self.base_field.from_db_value(item, expression, connection) for item in value]
        return value

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, 'core.fields.ArrayField', args, kwargs
