import re

from splitter.core import netinfo

IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def test_default_route_address_is_a_usable_ipv4_or_none() -> None:
    ip = netinfo.default_route_ipv4()
    assert ip is None or (IPV4.match(ip) and not ip.startswith("127."))


def test_local_addresses_exclude_loopback_and_put_the_default_route_first() -> None:
    addrs = netinfo.local_ipv4_addresses()
    assert all(IPV4.match(a) and not a.startswith("127.") for a in addrs)
    assert len(addrs) == len(set(addrs))
    first = netinfo.default_route_ipv4()
    if first:
        assert addrs[0] == first


def test_usable_filter() -> None:
    assert not netinfo._usable("127.0.0.1")
    assert not netinfo._usable("169.254.10.1")
    assert not netinfo._usable("0.0.0.0")
    assert netinfo._usable("192.168.1.23")
