from datetime import date, datetime, time

from utils.db import db

class BaseModel(db.Model):
    __abstract__ = True

    def to_dict(self):
        result = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, (datetime, date, time)):
                value = value.isoformat()
            result[column.name] = value
        return result
