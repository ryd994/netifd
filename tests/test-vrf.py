#!/usr/bin/env python3
"""Compile focused VRF regressions from the production C functions."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(file, name):
    text = (ROOT / file).read_text()
    start = text.index(name + "(")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


COMMON = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>
#include <unistd.h>
#define DEVADDR_FAMILY 1
#define DEVADDR_INET6 1
#define DEVROUTE_TABLE 2
#define DEVROUTE_SRCTABLE 4
#define DEVROUTE_METRIC 8
struct device { unsigned int vrf_table; };
struct interface {
    unsigned int metric, ip4table, ip6table;
    struct { struct device *dev; } l3_dev;
};
struct device_route { unsigned int flags, table, metric; };
"""


def run(source, *args):
    with tempfile.TemporaryDirectory(prefix="netifd-vrf-") as directory:
        path = Path(directory)
        (path / "test.c").write_text(source)
        subprocess.run(["cc", "-std=gnu99", "-Wall", "-Wextra", "-Werror",
                        str(path / "test.c"), "-o", str(path / "test")],
                       check=True)
        subprocess.run([str(path / "test"), *args], check=True)


run(COMMON + "static void\n" +
    function("interface-ip.c", "interface_set_route_info") + r"""
int main(void)
{
    struct device dev = { .vrf_table = 512 };
    struct interface iface = { .metric = 7, .l3_dev.dev = &dev };
    struct device_route route = {};
    interface_set_route_info(&iface, &route);
    assert(route.table == 512 && route.metric == 7);
    assert(route.flags & DEVROUTE_SRCTABLE);
    route = (struct device_route){ .flags = DEVADDR_INET6 };
    interface_set_route_info(&iface, &route);
    assert(route.table == 512);
    iface.ip4table = 42;
    route = (struct device_route){};
    interface_set_route_info(&iface, &route);
    assert(route.table == 42);
    route = (struct device_route){ .flags = DEVROUTE_TABLE, .table = 254 };
    interface_set_route_info(&iface, &route);
    assert(route.table == 254);
    route = (struct device_route){ .flags = DEVROUTE_METRIC, .metric = 99 };
    interface_set_route_info(&iface, &route);
    assert(route.metric == 99);
    iface.ip4table = 0;
    dev.vrf_table = 0;
    interface_set_route_info(&iface, &route);
    assert(route.table == 0 && !(route.flags & DEVROUTE_SRCTABLE));
    iface.l3_dev.dev = NULL;
    interface_set_route_info(&iface, &route);
    assert(route.table == 0);
    interface_set_route_info(NULL, &route);
    return 0;
}
""")

with tempfile.TemporaryDirectory(prefix="netifd-sysfs-") as directory:
    root = Path(directory)
    physical = root / "devices/pci/net/sta0"
    physical.mkdir(parents=True)
    (root / "class/net").mkdir(parents=True)
    (root / "class/net/sta0").symlink_to(physical)
    (physical / "master").symlink_to("../../../../virtual/net/vrf-test")
    run(COMMON + "static const char *sysfs_path;\nstatic char *" +
        function("system-linux.c", "system_get_vrf") + r"""
int main(int argc, char **argv)
{
    char buf[256];
    assert(argc == 2);
    sysfs_path = argv[1];
    assert(!strcmp(system_get_vrf("sta0", buf, sizeof(buf)), "vrf-test"));
    assert(system_get_vrf("missing", buf, sizeof(buf)) == NULL);
    assert(system_get_vrf("sta0", buf, 1) == NULL);
    assert(system_get_vrf("sta0", buf, 8) == NULL);
    return 0;
}
""", directory)

run(r"""
#include <assert.h>
#include <stdbool.h>
#define DEVICE 0
#define DEV_EVENT_TOPO_CHANGE 1
#define DEV_EVENT_VRF_CHANGE 2
#define D(...) do {} while (0)
struct device {
    unsigned int vrf_table;
    struct { bool auth; } settings;
    bool auth_status;
    const char *ifname;
};
struct device_user { struct device *dev; };
struct vrf_state { struct device dev; unsigned int table; int n_failed, n_present; };
struct vrf_member {
    struct vrf_state *vst;
    struct device_user dev;
    bool present, active;
};
static int sequence, attach, claim, error_attach, error_claim, releases;
static int vrf_enable_interface(struct vrf_state *vst)
{ (void)vst; return 0; }
static int system_vrf_addif(struct device *vrf, struct device *dev)
{ (void)vrf; (void)dev; attach = ++sequence; return error_attach; }
static int device_claim(struct device_user *dep)
{
    claim = ++sequence;
    if (!dep->dev->settings.auth) {
        assert(attach && attach < claim);
        assert(dep->dev->vrf_table == 512);
    }
    return error_claim;
}
static void device_release(struct device_user *dep)
{ (void)dep; releases++; }
static void vrf_disable_member(struct vrf_member *vm, bool keep)
{ (void)keep; vm->active = false; vm->dev.dev->vrf_table = 0; }
static void device_set_present(struct device *dev, bool value)
{ (void)dev; assert(value); }
static void device_broadcast_event(struct device *dev, int event)
{ (void)dev; assert(event == DEV_EVENT_TOPO_CHANGE || event == DEV_EVENT_VRF_CHANGE); }
""" + "static int\n" + function("vrf.c", "vrf_enable_member") + r"""
int main(void)
{
    struct device dev = {};
    struct vrf_state vst = { .table = 512, .n_present = 1 };
    struct vrf_member vm = { .vst = &vst, .dev.dev = &dev, .present = true };
    assert(!vrf_enable_member(&vm));
    assert(vm.active && dev.vrf_table == 512);
    int calls = sequence;
    assert(!vrf_enable_member(&vm));
    assert(sequence == calls);
    vm.active = false;
    dev.vrf_table = 0;
    error_attach = -1;
    assert(vrf_enable_member(&vm) == -1);
    assert(!vm.present && !dev.vrf_table && vst.n_failed == 1 && releases == 1);
    vm.present = true;
    vst.n_present = 1;
    error_attach = 0;
    error_claim = -1;
    assert(vrf_enable_member(&vm) == -1);
    assert(!vm.active && !vm.present && !dev.vrf_table);
    vm.present = true;
    vst.n_present = 1;
    dev.settings.auth = true;
    error_claim = 0;
    calls = attach;
    assert(vrf_enable_member(&vm) == -1);
    assert(attach == calls && vm.present && !vm.active);
    dev.auth_status = true;
    assert(!vrf_enable_member(&vm));
    assert(vm.active && dev.vrf_table == 512);
    return 0;
}
""")

interface_source = (ROOT / "interface.c").read_text()
refresh_start = interface_source.index("\tcase DEV_EVENT_VRF_CHANGE:")
refresh_end = interface_source.index("\n\tdefault:", refresh_start)
run(r"""
#include <assert.h>
#include <stdbool.h>
#define DEV_EVENT_VRF_CHANGE 1
#define IFS_UP 2
struct ip { bool enabled; };
struct interface { int state; struct ip config_ip, proto_ip; };
static int calls;
static bool states[4];
static void interface_ip_set_enabled(struct ip *ip, bool enabled)
{
    states[calls++] = enabled;
    ip->enabled = enabled;
}
static void refresh(struct interface *iface)
{
    switch (DEV_EVENT_VRF_CHANGE) {
""" + interface_source[refresh_start:refresh_end] + r"""
    }
}
int main(void)
{
    struct interface iface = { .state = IFS_UP, .config_ip.enabled = true };
    refresh(&iface);
    assert(calls == 4 && !states[0] && !states[1] && states[2] && !states[3]);
    assert(iface.config_ip.enabled && !iface.proto_ip.enabled);
    calls = 0;
    iface.state = 0;
    refresh(&iface);
    assert(!calls);
    return 0;
}
""")

print("VRF route precedence, member lifecycle, IP refresh and master lookup: OK")
