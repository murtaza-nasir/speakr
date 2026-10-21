"""Migrate legacy speaker data in an owned PostgreSQL schema, never a live DB."""
import os
import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine, inspect, text

from src.database import db
from src.init_db import initialize_database
from src.models import ManualVoiceSample, ManualVoiceSampleReceipt, Speaker, User


@pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URI','').startswith('postgresql'),
                    reason='isolated TEST_DATABASE_URI PostgreSQL required')
def test_legacy_postgres_rows_survive_additive_manual_tables_and_reopen():
    uri=os.environ['TEST_DATABASE_URI']
    schema='manual_migration_'+uuid.uuid4().hex
    owner=create_engine(uri)
    with owner.begin() as connection:
        connection.execute(text('CREATE SCHEMA '+schema))
    def application():
        app=Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI=uri,SQLALCHEMY_TRACK_MODIFICATIONS=False,
                          SQLALCHEMY_ENGINE_OPTIONS={'connect_args':{'options':'-csearch_path='+schema}},TESTING=True)
        db.init_app(app)
        return app
    def rows():
        return tuple(db.session.execute(text('SELECT id,name,average_embedding,embedding_count FROM speaker')).all())
    try:
        legacy=application()
        with legacy.app_context():
            tables=[t for t in db.metadata.sorted_tables if not t.name.startswith('manual_voice_')]
            db.metadata.create_all(db.engine,tables=tables)
            user=User(username='legacy-pg',email='legacy-pg@example.test',password='fixture')
            db.session.add(user);db.session.flush()
            db.session.add(Speaker(user_id=user.id,name='Legacy average',average_embedding=b'\x00\x00\x80?',embedding_count=3))
            db.session.commit()
            before=rows()
            initialize_database(legacy)
            assert {'manual_voice_sample','manual_voice_sample_receipt'}<=set(inspect(db.engine).get_table_names())
            assert rows()==before
            assert ManualVoiceSample.query.count()==ManualVoiceSampleReceipt.query.count()==0
            initialize_database(legacy)
            assert rows()==before
            db.session.remove();db.engine.dispose()
        reopened=application()
        with reopened.app_context():
            assert rows()==before
            assert ManualVoiceSample.query.count()==ManualVoiceSampleReceipt.query.count()==0
            db.session.remove();db.engine.dispose()
    finally:
        with owner.begin() as connection:
            connection.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        owner.dispose()
