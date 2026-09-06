"""Run against a disposable local MySQL database; never uses an instance database."""
import importlib.util
import re
import sys
import uuid
from types import SimpleNamespace
import pymysql

spec = importlib.util.spec_from_file_location('registration', sys.argv[1])
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)
name = 'mcd_registration_test_' + uuid.uuid4().hex[:10]
connection = dict(user='root', unix_socket='/run/mysqld/mysqld.sock', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
admin = pymysql.connect(**connection)
try:
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE `{name}`')
    class Database:
        cfg = SimpleNamespace(table_prefix='test_')
        def _connect(self):
            return pymysql.connect(**connection, database=name)
        def _safe_table(self, value):
            assert re.fullmatch('[a-zA-Z0-9_]+', value)
            return value
    db = Database()
    with db._connect() as conn, conn.cursor() as cur:
        cur.execute('CREATE TABLE test_plugins (id INT PRIMARY KEY AUTO_INCREMENT, bundle VARCHAR(255) UNIQUE) ENGINE=InnoDB')
        cur.execute('''CREATE TABLE test_plugin_integration_settings (id INT PRIMARY KEY AUTO_INCREMENT,
            plugin_id INT NULL, name VARCHAR(255), api_keys LONGBLOB, is_published TINYINT,
            FOREIGN KEY (plugin_id) REFERENCES test_plugins(id) ON DELETE CASCADE) ENGINE=InnoDB''')
        cur.execute("INSERT INTO test_plugins (bundle) VALUES ('DemoBundle'), ('OtherBundle')")
        cur.execute("INSERT INTO test_plugin_integration_settings (plugin_id,name,api_keys,is_published) VALUES (1,'Demo',%s,0),(2,'Other',%s,1)", (b'ciphertext-demo\x00bytes', b'ciphertext-other'))
        cur.execute('SELECT id,name,api_keys,is_published FROM test_plugin_integration_settings ORDER BY id')
        original = cur.fetchall()
    assert registration.unregister(db, ['DemoBundle']) == 2
    with db._connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT id,name,api_keys,is_published FROM test_plugin_integration_settings ORDER BY id')
        assert cur.fetchall() == original
        cur.execute("INSERT INTO test_plugins (bundle) VALUES ('DemoBundle')")
    assert registration.restore(db, {'DemoBundle'}) == 1
    with db._connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT id,name,api_keys,is_published FROM test_plugin_integration_settings ORDER BY id')
        assert cur.fetchall() == original
    assert registration.unregister(db, ['DemoBundle']) == 2
    assert registration.unregister(db, ['DemoBundle']) == 0
    assert registration.unregister(db, ['DemoBundle'], purge=True) == 1
    assert registration.unregister(db, ['DemoBundle'], purge=True) == 0
    with db._connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT id,name,api_keys,is_published FROM test_plugin_integration_settings ORDER BY id')
        assert cur.fetchall() == [original[1]]
    assert registration.unregister(db, ['OtherBundle'], purge=True) == 2
    print('PASS MySQL remove, byte-exact settings preservation, disabled state, reinstall, purge after remove, idempotency, unrelated plugin isolation')
finally:
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE `{name}`')
    admin.close()
