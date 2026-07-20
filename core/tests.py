"""Core tests: role-based access control and multi-tenant schema isolation.

The isolation tests are the important ones — they prove that data written in one
workspace's Postgres schema is invisible from another, which is the whole safety
promise of the schema-per-tenant design.
"""
from django.db import connection
from django.http import HttpResponse
from django.test import SimpleTestCase, TransactionTestCase
from django_tenants.utils import tenant_context

from modules.crm.models import Company

from .models import Domain, Membership, Workspace
from .rbac import can_edit, require_role


class _FakeRequest:
    """Minimal stand-in carrying the attribute the RBAC helpers read."""

    def __init__(self, membership):
        self.membership = membership


class RoleHierarchyTests(SimpleTestCase):
    """`Membership.can` / `.level` are pure logic — no database needed."""

    def test_levels_are_ordered(self):
        owner = Membership(role=Membership.ROLE_OWNER)
        viewer = Membership(role=Membership.ROLE_VIEWER)
        self.assertGreater(owner.level, viewer.level)

    def test_can_respects_the_hierarchy(self):
        admin = Membership(role=Membership.ROLE_ADMIN)
        self.assertTrue(admin.can(Membership.ROLE_MEMBER))
        self.assertTrue(admin.can(Membership.ROLE_ADMIN))
        self.assertFalse(admin.can(Membership.ROLE_OWNER))

    def test_member_cannot_act_as_admin(self):
        member = Membership(role=Membership.ROLE_MEMBER)
        self.assertTrue(member.can(Membership.ROLE_MEMBER))
        self.assertFalse(member.can(Membership.ROLE_ADMIN))


class RbacHelperTests(SimpleTestCase):
    def test_can_edit_allows_member_and_above(self):
        self.assertTrue(can_edit(_FakeRequest(Membership(role=Membership.ROLE_MEMBER))))
        self.assertTrue(can_edit(_FakeRequest(Membership(role=Membership.ROLE_OWNER))))

    def test_can_edit_blocks_viewers_and_anonymous(self):
        self.assertFalse(can_edit(_FakeRequest(Membership(role=Membership.ROLE_VIEWER))))
        self.assertFalse(can_edit(_FakeRequest(None)))

    def test_require_role_forbids_below_minimum(self):
        @require_role(Membership.ROLE_ADMIN)
        def view(request):
            return HttpResponse("ok")

        allowed = view(_FakeRequest(Membership(role=Membership.ROLE_ADMIN)))
        denied = view(_FakeRequest(Membership(role=Membership.ROLE_MEMBER)))
        missing = view(_FakeRequest(None))

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(missing.status_code, 403)


class SchemaIsolationTests(TransactionTestCase):
    """Two real tenant schemas; data in one must never surface in the other.

    Uses TransactionTestCase (not TestCase) because creating a workspace runs the
    tenant migrations as schema DDL, which can't live inside the test's atomic
    wrapper.
    """

    def setUp(self):
        connection.set_schema_to_public()
        self.ws_a = Workspace(schema_name="test_iso_a", name="Alpha")
        self.ws_a.save()
        Domain.objects.create(tenant=self.ws_a, domain="a.iso.test")
        self.ws_b = Workspace(schema_name="test_iso_b", name="Bravo")
        self.ws_b.save()
        Domain.objects.create(tenant=self.ws_b, domain="b.iso.test")

    def tearDown(self):
        connection.set_schema_to_public()
        for ws in (self.ws_a, self.ws_b):
            try:
                ws.delete()  # drops the schema + removes the public-side rows
            except Exception:
                pass

    def _schema_exists(self, name):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                [name],
            )
            return cur.fetchone() is not None

    def test_records_are_isolated_between_schemas(self):
        with tenant_context(self.ws_a):
            Company.objects.create(workspace=self.ws_a, name="Acme")
        with tenant_context(self.ws_b):
            Company.objects.create(workspace=self.ws_b, name="Globex")

        # Even the UNSCOPED manager only sees the current schema's rows, so this
        # proves the boundary is the schema itself, not just the manager filter.
        with tenant_context(self.ws_a):
            self.assertEqual(
                list(Company.all_objects.values_list("name", flat=True)), ["Acme"]
            )
        with tenant_context(self.ws_b):
            self.assertEqual(
                list(Company.all_objects.values_list("name", flat=True)), ["Globex"]
            )

    def test_deleting_a_workspace_drops_its_schema(self):
        self.assertTrue(self._schema_exists("test_iso_a"))

        self.ws_a.delete()

        self.assertFalse(self._schema_exists("test_iso_a"))
        # The other tenant is untouched.
        self.assertTrue(self._schema_exists("test_iso_b"))
