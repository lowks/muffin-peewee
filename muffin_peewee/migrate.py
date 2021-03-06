import datetime as dt
from cached_property import cached_property
from os import path as op, listdir as ls, makedirs as md
from re import compile as re
from shutil import copy

import peewee as pw
from playhouse.migrate import SchemaMigrator, Operation


MIGRATE_TEMPLATE = op.join(
    op.abspath(op.dirname(__file__)), 'migration.tmpl'
)


def exec_in(codestr, glob, loc=None):
    code = compile(codestr, '<string>', 'exec', dont_inherit=True)
    exec(code, glob, loc)


class MigrationError(Exception):

    """ Presents an error during migration process. """


class Router(object):

    """ Control migrations. """

    filemask = re(r"[\d]{3}_[^\.]+\.py")

    def __init__(self, plugin):
        self.app = plugin.app
        self.database = plugin.database
        self.migrate_dir = plugin.options['migrations_path']

    @cached_property
    def model(self):
        """ Ensure that migrations has prepared to run. """
        # Initialize MigrationHistory model
        MigrateHistory._meta.database = self.app.plugins.peewee.database
        try:
            MigrateHistory.create_table()
        except pw.DatabaseError:
            self.database.rollback()
        return MigrateHistory

    @property
    def fs_migrations(self):
        if not op.exists(self.migrate_dir):
            self.app.logger.warn('Migration directory: %s does not exists.', self.migrate_dir)
            md(self.migrate_dir)
        return sorted(''.join(f[:-3]) for f in ls(self.migrate_dir) if self.filemask.match(f))

    @property
    def db_migrations(self):
        return [mm.name for mm in self.model.select()]

    @property
    def diff(self):
        dbms = set(self.db_migrations)
        return [name for name in self.fs_migrations if name not in dbms]

    def create(self, name='auto'):
        """ Create a migration. """

        self.app.logger.info('Create a migration "%s"', name)

        num = len(self.fs_migrations)
        prefix = '{:03}_'.format(num)
        name = prefix + name + '.py'
        path = copy(MIGRATE_TEMPLATE, op.join(self.migrate_dir, name))

        self.app.logger.info('Migration has created %s', path)
        return path

    def run(self, name=None):
        """ Run migrations. """

        self.app.logger.info('Start migrations')

        migrator = Migrator(self.database)
        diff = self.diff

        if not diff:
            self.app.logger.info('Nothing to migrate')
            return None

        for mname in self.fs_migrations:
            self.run_one(mname, migrator, mname not in diff)
            if name and name == mname:
                break

    def run_one(self, name, migrator, fake=True):
        """ Run a migration. """

        if not fake:
            self.app.logger.info('Run "%s"', name)

        try:
            with open(op.join(self.migrate_dir, name + '.py')) as f:
                code = f.read()
                scope = {}
                exec_in(code, scope)
                migrate = scope.get('migrate', lambda m: None)
                migrate(migrator, self.database, app=self.app)
                if fake:
                    migrator.clean()
                    return migrator

                self.app.logger.info('Start migration %s', name)
                with self.database.transaction():
                    migrator.run()
                    self.model.create(name=name)
                    self.app.logger.info('Migrated %s', name)

        except Exception as exc:
            self.database.rollback()
            self.app.logger.error(exc)
            raise


class MigrateHistory(pw.Model):

    """ Presents the migrations in database. """

    name = pw.CharField()
    migrated_at = pw.DateTimeField(default=dt.datetime.utcnow)

    def __unicode__(self):
        return self.name


def get_model(method):
    def wrapper(migrator, model, *args, **kwargs):
        if isinstance(model, str):
            return method(migrator, migrator.orm[model], *args, **kwargs)
        return method(migrator, model, *args, **kwargs)
    return wrapper


class Migrator(object):

    """ Provide migrations. """

    def __init__(self, database):
        if isinstance(database, pw.Proxy):
            database = database.obj

        self.database = database
        self.orm = dict()
        self.ops = list()
        self.migrator = SchemaMigrator.from_database(self.database)

    def run(self):
        for operation in self.ops:
            if isinstance(operation, Operation):
                operation.run()
            else:
                operation()
        self.clean()

    def clean(self):
        self.ops = list()

    def create_table(self, model):
        """ >> migrator.create_table(model) """
        self.orm[model._meta.db_table] = model
        model._meta.database = self.database
        self.ops.append(lambda: self.database.create_table(model))
        return model

    @get_model
    def drop_table(self, model, cascade=True):
        """ >> migrator.drop_table(model, cascade=True) """
        del self.orm[model._meta.db_table]
        self.ops.append(lambda: self.database.drop_table(model, cascade=cascade))
        return None

    @get_model
    def add_columns(self, model, **fields):
        for name, field in fields.items():
            field.add_to_class(model, name)
            self.ops.append(self.migrator.add_column(model._meta.db_table, name, field))
        return model

    @get_model
    def drop_columns(self, model, *names, cascade=True):
        fields = [field for field in model._meta.fields.values() if field.name in names]
        for field in fields:
            self.__del_field__(model, field)
            self.ops.append(
                self.migrator.drop_column(
                    model._meta.db_table, field.db_column, cascade=cascade))
        return model

    def __del_field__(self, model, field):
        del model._meta.fields[field.name]
        del model._meta.columns[field.db_column]
        delattr(model, field.name)
        if isinstance(field, pw.ForeignKeyField):
            delattr(field.rel_model, field.related_name)
            del field.rel_model._meta.reverse_rel[field.related_name]

    @get_model
    def rename_column(self, model, old_name, new_name):
        field = model._meta.fields[old_name]
        self.__del_field__(model, field)
        field.name = field.db_column = new_name
        field.add_to_class(model, new_name)
        self.ops.append(self.migrator.rename_column(model._meta.db_table, old_name, new_name))
        return model

    @get_model
    def rename_table(self, model, new_name):
        del self.orm[model._meta.db_table]
        model._meta.db_table = new_name
        self.orm[model._meta.db_table] = model
        self.ops.append(self.migrator.rename_table(model._meta.db_table, new_name))
        return model

    @get_model
    def add_index(self, model, *columns, unique=False):
        model._meta.indexes.append((columns, unique))
        self.ops.append(self.migrator.add_index(model._meta.db_table, columns, unique=unique))
        return model

    @get_model
    def drop_index(self, model, index_name):
        self.ops.append(self.migrator.drop_index(model._meta.db_table, index_name))
        return model

    @get_model
    def add_not_null(self, model, name):
        self.ops.append(self.migrator.add_not_null(model._meta.db_table, name))
        return model

    @get_model
    def drop_not_null(self, model, name):
        self.ops.append(self.migrator.drop_not_null(model._meta.db_table, name))
        return model
