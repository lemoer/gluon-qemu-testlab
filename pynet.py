#!/bin/python36

import os
import sys
import time
import shutil
import asyncio
import socket
import asyncssh
import subprocess
from operator import itemgetter

#import logging
#
#logging.basicConfig(
#    level=logging.DEBUG,
#    format='%(levelname)7s: %(message)s',
#    stream=sys.stderr,
#)

image = "image.img"
SSH_KEY_FILE = './ssh/id_rsa.key'
SSH_PUBKEY_FILE = SSH_KEY_FILE + '.pub'
NEXT_NODE_ADDR = 'fdca:ffee:8::1'
SITE_LOCAL_PREFIX = 'fdca:ffee:8:0'

class Node():

    max_id = 0
    max_port = 17321
    all_nodes = []

    def __init__(self):
        Node.max_id += 1
        Node.all_nodes += [self]
        self.id = Node.max_id
        self.hostname = f'node{self.id}'
        self.mesh_links = []
        self.if_index_max = 1
        self.uci_sets = []
        self.uci_commits = []
        self.site_local_prefix = SITE_LOCAL_PREFIX
        self.next_node_addr = NEXT_NODE_ADDR
        self.domain_code = None

    def add_mesh_link(self, peer, _is_peer=False, _port=None):
        self.if_index_max += 1
        ifname = f'eth{self.if_index_max}'
        if _port is None:
            Node.max_port += 1
            port = Node.max_port
            conn_type = 'listen'
        else:
            port = _port
            conn_type = 'connect'
        self.mesh_links.append((ifname, peer, conn_type, port))
        if not _is_peer:
            peer.add_mesh_link(self, _is_peer=True, _port=port)
        return ifname

    def set_fastd_secret(self, secret):
        assert(type(secret) == str)
        assert(len(secret) == 64)
        for k in secret:
            assert(k in "1234567890abcdef")
        self.uci_set('fastd', 'mesh_vpn', 'secret', secret)
        self.uci_set('fastd', 'mesh_vpn', 'enabled', 1)

    def uci_set(self, config, section, option, value):
        self.uci_sets += ["uci set {}.{}.{}='{}'".format(
            config, section, option, value)]
        self.uci_commits += ["uci commit {}".format(config)]

    def set_domain(self, domain_code):
        self.uci_set('gluon', 'core', 'domain', domain_code)
        self.domain_code = domain_code

    @property
    def if_client(self):
        return "client" + str(self.id)
        


class MobileClient():

    max_id = 0

    def __init__(self):
        MobileClient.max_id += 1
        self.current_node = None
        self.ifname_peer = f'mobile{MobileClient.max_id}_peer'
        self.ifname = f'mobile{MobileClient.max_id}'
        self.netns = f'mobile{MobileClient.max_id}'

        run(f'ip netns add {self.netns}')
        run_in_netns(self.netns, f'ip link del {self.ifname}')
        run(f'ip link add {self.ifname} type veth peer name {self.ifname_peer}')
        run(f'ip link set {self.ifname} address de:ad:be:ee:ff:01 netns {self.netns} up')
        run(f'ip link set {self.ifname} up')

    def move_to(self, node):
        netns_new = "%s_client" % node.hostname
        bridge_new = "br_" + node.if_client

        if self.current_node is not None:
            netns_old = "%s_client" % self.current_node.hostname
            run_in_netns(netns_old, f'ip link set {self.ifname_peer} netns {netns_new} up')
        else:
            run(f'ip link set {self.ifname_peer} netns {netns_new} up')

        run_in_netns(netns_new, f'ip link set {self.ifname_peer} master {bridge_new}')

        self.current_node = node

def run(cmd):
    subprocess.run(cmd, shell=True)

def run_in_netns(netns, cmd):
    subprocess.run(f'ip netns exec {netns} ' + cmd, shell=True)

stdout_buffers = {}
processes = {}

@asyncio.coroutine
def gen_qemu_call(image, node):

    shutil.copyfile('./' + image, './images/%02x.img' % node.id)

    # TODO: machine identifier
    host_id = 1
    nat_mac = "52:54:%02x:%02x:34:%02x" % (host_id, node.id, 1)
    client_mac = "52:54:%02x:%02x:34:%02x" % (host_id, node.id, 2)

    mesh_ifaces = []
    mesh_id = 1
    for _, _, conn_type, port in node.mesh_links:
        if conn_type not in ['listen', 'connect']:
            raise ValueError('conn_type invalid: ' + str(conn_type))

        if conn_type == 'connect':
            yield from wait_bash_cmd(f'while ! ss -tlp4n | grep ":{port}" &>/dev/null; do sleep 1; done;')

        mesh_ifaces += [
            '-device', ('rtl8139,addr=0x%02x,netdev=mynet%d,id=m_nic%d,mac=' + \
                "52:54:%02x:%02x:34:%02x") % (10 + mesh_id, mesh_id, mesh_id, host_id, node.id, 10 + mesh_id),
            '-netdev', 'socket,id=mynet%d,%s=:%d' % (mesh_id, conn_type, port)
        ]

        mesh_id += 1

    call = ['-nographic',
            '-enable-kvm',
            '-netdev', 'user,id=hn1',
            '-device', 'rtl8139,addr=0x06,netdev=hn1,id=nic1,mac=' + nat_mac,
            '-netdev', 'tap,id=hn2,script=no,downscript=no,ifname=%s' % node.if_client,
            '-device', 'rtl8139,addr=0x05,netdev=hn2,id=nic2,mac=' + client_mac]

    # '-d', 'guest_errors', '-d', 'cpu_reset', '-gdb', 'tcp::' + str(3000 + node.id),
    args = ['qemu-system-x86_64',
            '-drive', 'format=raw,file=./images/%02x.img' % node.id] + call + mesh_ifaces

    fifo_path = './fifos/%02x' % node.id
    if os.path.exists(fifo_path):
        os.remove(fifo_path)
    os.mkfifo(fifo_path)
    stdin = os.open(fifo_path, os.O_NONBLOCK | os.O_RDONLY)
    process = asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE, stdin=stdin)

    processes[node.id] = yield from process

async def ssh_call(p, cmd):
    res = await p.run(cmd)
    return res.stdout

async def set_mesh_devs(p, devs):
    for d in devs:
        await ssh_call(p, f"uci set network.{d}_mesh=interface")
        await ssh_call(p, f"uci set network.{d}_mesh.auto=1")
        await ssh_call(p, f"uci set network.{d}_mesh.proto=gluon_wired")
        await ssh_call(p, f"uci set network.{d}_mesh.ifname={d}")

        # allow vxlan in firewall
        await ssh_call(p, f'uci add_list firewall.wired_mesh.network={d}_mesh')

    await ssh_call(p, 'uci commit network')
    await ssh_call(p, 'uci commit firewall')

async def add_ssh_key(p):
    # TODO: this removes baked in ssh keys :/
    with open(SSH_PUBKEY_FILE) as f:
        content = f.read()
        await ssh_call(p, f'cat >> /etc/dropbear/authorized_keys <<EOF\n{content}')

@asyncio.coroutine
def wait_bash_cmd(cmd):
    create = asyncio.create_subprocess_exec("/bin/bash", '-c', cmd)
    proc = yield from create

    # Wait for the subprocess exit
    yield from proc.wait()

async def install_client(initial_time, node):
    clientname = f"client{node.id}"
    dbg = debug_print(initial_time, clientname)

    ifname = node.if_client

    # client iface link local addr
    host_id = 1
    lladdr = "fe80::5054:%02xff:fe%02x:34%02x" % (host_id, node.id, 2)

    dbg(f'waiting for iface {ifname} to appear')
    await wait_bash_cmd(f'while ! ip link show dev {ifname} &>/dev/null; do sleep 1; done;')

    # set mac of client tap iface on host system
    client_iface_mac = "aa:54:%02x:%02x:34:%02x" % (host_id, node.id, 2)
    run(f'ip link set {ifname} address {client_iface_mac}')

    run(f'ip link set {ifname} up')
    await wait_bash_cmd(f'while ! ping -c 1 {lladdr}%{ifname} &>/dev/null; do sleep 1; done;')
    dbg(f'iface {ifname} appeared')

    # create netns
    netns = "%s_client" % node.hostname
    # TODO: delete them correctly
    # Issue with mountpoints yet http://man7.org/linux/man-pages/man7/mount_namespaces.7.html
    run(f'ip netns add {netns}')
    gen_etc_hosts_for_netns(netns)

    # wait for ssh TODO: hacky
    await wait_bash_cmd(f'while ! nc {lladdr}%{ifname} 22 -w 1 > /dev/null; do sleep 1; done;')

    # node setup setup needs to be done here
    print(f'{lladdr}%{ifname}')
    addr = f'{lladdr}%{ifname}'
    #addr = socket.getaddrinfo(f'{lladdr}%{ifname}', 22, socket.AF_INET6, socket.SOCK_STREAM)[0]
    async with asyncssh.connect(addr, username='root', known_hosts=None) as conn:
        await config_node(initial_time, node, conn)
    dbg(f'{node.hostname} configured')

    # move iface to netns
    dbg(f'moving {ifname} to netns {netns}')
    run(f'ip link set netns {netns} dev {ifname}')
    run_in_netns(netns, f'ip link set lo up')
    run_in_netns(netns, f'ip link set {ifname} up')
    run_in_netns(netns, f'ip link delete br_{ifname} type bridge 2> /dev/null || true') # force deletion
    run_in_netns(netns, f'ip link add name br_{ifname} type bridge')
    run_in_netns(netns, f'ip link set {ifname} master br_{ifname}')
    run_in_netns(netns, f'ip link set br_{ifname} up')

    # spawn client shell
    shell = os.environ.get('SHELL') or '/bin/bash'
    spawn_in_tmux(clientname, f'ip netns exec {netns} {shell}')

    # spawn ssh shell
    ssh_opts = '-o UserKnownHostsFile=/dev/null ' + \
               '-o StrictHostKeyChecking=no ' + \
               f'-i {SSH_KEY_FILE} '
    spawn_in_tmux(node.hostname, f'ip netns exec {netns} /bin/bash -c "while ! ssh {ssh_opts} root@{node.next_node_addr}; do sleep 1; done"')

def spawn_in_tmux(title, cmd):
    run(f'tmux -S test new-window -d -n {title} {cmd}')

@asyncio.coroutine
def read_to_buffer(node):
    while processes.get(node.id) is None:
        yield from asyncio.sleep(0)
    process = processes[node.id]
    stdout_buffers[node.id] = b""
    with open(f'logs/{node.hostname}.log', 'wb') as f1:
        while True:
            b = yield from process.stdout.read(1) # TODO: is this unbuffered?
            stdout_buffers[node.id] += b
            f1.write(b)
            if b == b'\n':
                f1.flush()

@asyncio.coroutine
def wait_for(node, b):
    i = node.id
    while stdout_buffers.get(i) is None:
        yield from asyncio.sleep(0)
    while True:
        if b.encode('utf-8') in stdout_buffers[i]:
            return
        yield from asyncio.sleep(0)

async def add_hosts(p):
    await ssh_call(p, f'cat >> /etc/hosts <<EOF\n{host_entries}\n')
    await ssh_call(p, f'cat >> /etc/bat-hosts <<EOF\n{bathost_entries}\n')

def debug_print(since, hostname):
    def printfn(message):
        delta = time.time() - since
        print(f'[{delta:>8.2f} | {hostname:<9}] {message}')
    return printfn

async def config_node(initial_time, node, ssh_conn):

    dbg = debug_print(initial_time, node.hostname)

    p = ssh_conn

    mesh_ifaces = list(map(itemgetter(0), node.mesh_links))

    await set_mesh_devs(p, mesh_ifaces)
    await add_hosts(p)
    await ssh_call(p, f'pretty-hostname {node.hostname}')
    await add_ssh_key(p)

    # do uci configs
    for cmd in node.uci_sets:
        await ssh_call(p, cmd)
    for cmd in set(node.uci_commits):
        await ssh_call(p, cmd)

    if node.domain_code is not None:
        await ssh_call(p, "gluon-reconfigure")

    # reboot to operational mode
    await ssh_call(p, 'uci set gluon-setup-mode.@setup_mode[0].configured=\'1\'')
    await ssh_call(p, 'uci commit gluon-setup-mode')
    await ssh_call(p, 'reboot')

    await wait_for(node, 'reboot: Restarting system')
    dbg('leaving config mode (reboot)')
    # flush buffer
    stdout_buffers[node.id] = b''.join(stdout_buffers[node.id].split(b'reboot: Restarting system')[1:])
    await wait_for(node, 'Please press Enter to activate this console.')
    dbg('console appeared (again)')

    #ssh_call(p, 'uci set fastd.mesh_vpn.enabled=0')
    #ssh_call(p, 'uci commit fastd')
    #ssh_call(p, '/etc/init.d/fastd stop mesh_vpn')

def gen_etc_hosts_for_netns(netns):
    # use /etc/hosts and extend it
    with open('/etc/hosts') as h:
        if not os.path.exists('/etc/netns/'):
            os.mkdir('/etc/netns')
        if not os.path.exists(f'/etc/netns/{netns}/'):
            os.mkdir(f'/etc/netns/{netns}')
        with open(f'/etc/netns/{netns}/hosts', 'w') as f:
            f.write(h.read())
            f.write('\n')
            f.write(host_entries)

host_entries = ""
bathost_entries = ""

def run_all():

    if os.environ.get('TMUX') is None and not 'notmux' in sys.argv:
        os.execl('/usr/bin/tmux', 'tmux', '-S', 'test', 'new', sys.executable, '-i', *sys.argv)

    # TODO: cd to project folder
    if not os.path.exists(SSH_PUBKEY_FILE):
        run(f'ssh-keygen -t rsa -f {SSH_KEY_FILE} -N \'\'')

    loop = asyncio.get_event_loop()

    host_id = 1
    global host_entries
    global bathost_entries

    for node in Node.all_nodes:
        host_entries += f"{node.site_local_prefix}:5054:{host_id}ff:fe{node.id:02x}:3402 {node.hostname}\n"
        client_name = node.hostname.replace('node', 'client')
        host_entries += f"{node.site_local_prefix}:a854:{host_id}ff:fe{node.id:02x}:3402 {client_name}\n"
        bathost_entries += f"52:54:{host_id:02x}:{node.id:02x}:34:02 {node.hostname}\n"
        bathost_entries += f"aa:54:{host_id:02x}:{node.id:02x}:34:02 {client_name}\n"

    bathost_entries += "de:ad:be:ee:ff:01 mobile1\n"


    for node in Node.all_nodes:
        loop.create_task(gen_qemu_call(image, node))
        loop.create_task(read_to_buffer(node))
        loop.create_task(install_client(initial_time, node))

    loop.run_forever()

def connect(a, b):
    a.add_mesh_link(b)

initial_time = time.time()