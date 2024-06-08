import datetime as dt
from contextlib import closing

from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.dispatch import receiver
from django.test import TestCase

from django_pgviews.signals import view_synced

from ..viewtest.models import RelatedView
from ..viewtest.tests import get_list_of_indexes
from .models import SchemaMonthlyObservationMaterializedView, SchemaMonthlyObservationView, SchemaObservation


class WeatherPinnedViewConnectionTest(TestCase):
    """Weather views should only return schema_db when pinned."""

    def test_schema_view_using_schema_db(self):
        self.assertEqual(SchemaMonthlyObservationView.get_view_connection(using="schema_db"), connections["schema_db"])

    def test_schema_view_using_default_db(self):
        self.assertIsNone(SchemaMonthlyObservationView.get_view_connection(using=DEFAULT_DB_ALIAS))

    def test_schema_materialized_view_using_schema_db(self):
        self.assertEqual(
            SchemaMonthlyObservationMaterializedView.get_view_connection(using="schema_db"), connections["schema_db"]
        )

    def test_schema_materialized_view_using_default_db(self):
        self.assertIsNone(SchemaMonthlyObservationMaterializedView.get_view_connection(using=DEFAULT_DB_ALIAS))

    def test_other_app_view_using_schema_db(self):
        self.assertIsNone(RelatedView.get_view_connection(using="schema_db"))

    def test_other_app_view_using_default_db(self):
        self.assertEqual(RelatedView.get_view_connection(using=DEFAULT_DB_ALIAS), connections["default"])


class SchemaTest(TestCase):
    """View.refresh() should automatically select the appropriate schema."""

    databases = {DEFAULT_DB_ALIAS, "schema_db"}

    def test_schemas(self):
        with closing(connections["schema_db"].cursor()) as cur:
            cur.execute("""SELECT schemaname FROM pg_tables WHERE tablename LIKE 'schemadbtest_schemaobservation';""")

            res = cur.fetchone()
            self.assertIsNotNone(res, "Can't find table schemadbtest_schemaobservation;")

            (schemaname,) = res
            self.assertEqual(schemaname, "other")

            cur.execute(
                """SELECT schemaname FROM pg_views WHERE viewname LIKE 'schemadbtest_schemamonthlyobservationview';"""
            )

            res = cur.fetchone()
            self.assertIsNotNone(res, "Can't find schemadbtest_schemamonthlyobservationview;")

            (schemaname,) = res
            self.assertEqual(schemaname, "other")

            cur.execute(
                """SELECT schemaname FROM pg_matviews WHERE matviewname LIKE 'schemadbtest_schemamonthlyobservationmaterializedview';"""
            )

            res = cur.fetchone()
            self.assertIsNotNone(res, "Can't find schemadbtest_schemamonthlyobservationmaterializedview.")

            (schemaname,) = res
            self.assertEqual(schemaname, "other")

            indexes = get_list_of_indexes(cur, SchemaMonthlyObservationMaterializedView)
            self.assertEqual(indexes, {"schemadbtes_date_9985f7_idx"})

    def test_view(self):
        SchemaObservation.objects.create(date=dt.date(2022, 1, 1), temperature=10)
        SchemaObservation.objects.create(date=dt.date(2022, 1, 3), temperature=20)
        self.assertEqual(SchemaMonthlyObservationView.objects.count(), 1)

    def test_mat_view_pre_refresh(self):
        SchemaObservation.objects.create(date=dt.date(2022, 1, 1), temperature=10)
        SchemaObservation.objects.create(date=dt.date(2022, 1, 3), temperature=20)
        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 0)

    def test_mat_view_refresh(self):
        SchemaObservation.objects.create(date=dt.date(2022, 1, 1), temperature=10)
        SchemaObservation.objects.create(date=dt.date(2022, 1, 3), temperature=20)
        SchemaMonthlyObservationMaterializedView.refresh()
        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 1)

    def test_view_exists_on_sync(self):
        synced = []

        @receiver(view_synced)
        def on_view_synced(sender, **kwargs):
            synced.append(sender)
            if sender == SchemaMonthlyObservationView:
                self.assertEqual(
                    dict(
                        {"status": "EXISTS", "has_changed": False},
                        update=False,
                        force=False,
                        signal=view_synced,
                        using="schema_db",
                    ),
                    kwargs,
                )
            if sender == SchemaMonthlyObservationMaterializedView:
                self.assertEqual(
                    dict(
                        {"status": "UPDATED", "has_changed": True},
                        update=False,
                        force=False,
                        signal=view_synced,
                        using="schema_db",
                    ),
                    kwargs,
                )

        call_command("sync_pgviews", database="schema_db", update=False)

        self.assertIn(SchemaMonthlyObservationView, synced)
        self.assertIn(SchemaMonthlyObservationMaterializedView, synced)

    def test_sync_pgviews_materialized_views_check_sql_changed(self):
        self.assertEqual(SchemaObservation.objects.count(), 0, "Test started with non-empty SchemaObservation")
        self.assertEqual(
            SchemaMonthlyObservationMaterializedView.objects.count(), 0, "Test started with non-empty mat view"
        )

        SchemaObservation.objects.create(date=dt.date(2022, 1, 1), temperature=10)

        # test regular behaviour, the mat view got recreated
        call_command("sync_pgviews", database="schema_db", update=False)  # uses default django setting, False
        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 1)

        # the mat view did not get recreated because the model hasn't changed
        SchemaObservation.objects.create(date=dt.date(2022, 2, 3), temperature=20)
        call_command("sync_pgviews", database="schema_db", update=False, materialized_views_check_sql_changed=True)
        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 1)

        # the mat view got recreated because the mat view SQL has changed

        # let's pretend the mat view in the DB is ordered by name, while the defined on models isn't
        with connections["schema_db"].cursor() as cursor:
            cursor.execute("DROP MATERIALIZED VIEW schemadbtest_schemamonthlyobservationmaterializedview CASCADE;")
            cursor.execute(
                """
                CREATE MATERIALIZED VIEW schemadbtest_schemamonthlyobservationmaterializedview as
                WITH summary AS (
                    SELECT
                        date_trunc('day', date) AS date,
                        count(*)
                    FROM schemadbtest_schemaobservation
                    GROUP BY 1
                    ORDER BY date
                ) SELECT
                    ROW_NUMBER() OVER () AS id,
                    date,
                    count
                FROM summary;
                """
            )

        call_command("sync_pgviews", update=False, materialized_views_check_sql_changed=True)
        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 2)

    def test_migrate_materialized_views_check_sql_changed_default(self):
        self.assertEqual(SchemaObservation.objects.count(), 0, "Test started with non-empty SchemaObservation")
        self.assertEqual(
            SchemaMonthlyObservationMaterializedView.objects.count(), 0, "Test started with non-empty mat view"
        )

        SchemaObservation.objects.create(date=dt.date(2022, 1, 1), temperature=10)

        call_command("migrate", database="schema_db")

        self.assertEqual(SchemaMonthlyObservationMaterializedView.objects.count(), 1)
