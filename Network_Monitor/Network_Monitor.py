from dotenv import load_dotenv
import os
import requests
import logging
import subprocess
import re
import socket
import time
import atexit
from scapy.all import sniff, IP, DNS, conf, DNSRR
from typing import Set, Optional, Dict
import json
import threading
from queue import Queue

whitelisted_ips = set()

def on_exit():
    global whitelisted_ips
    with open("clean_ips.txt", "w") as file:
        file.write("\n".join(whitelisted_ips))
atexit.register(on_exit)

def get_api():
    load_dotenv()
    API_KEY = os.getenv("API_KEY")
    return API_KEY

def IP_Lookup(api, ip):
    url = f"https://www.virustotal.com/api/v3/ip_addresses/{ip}"
    headers = {
    "accept": "application/json",
    "x-apikey": f"{api}"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        return None


def IP_Filter(data):
    sources = []
    if data is not None:
        for source, details in data["data"]["attributes"]["last_analysis_results"].items():
            if details["result"] == "malicious" or details["result"] == "malware":
                sources.append(source)
        if len(sources) == 0:
            return None
        else:
            return sources, data["data"]["attributes"]["whois"]
    return None
    

def create_logger():
    malicious_logger = logging.getLogger("malicious_ips")
    malicious_handler = logging.FileHandler("malicious_ips.log", delay=False)
    malicious_handler.setLevel(logging.WARNING)
    malicious_formatter = logging.Formatter("%(asctime)s\n%(message)s")
    malicious_handler.setFormatter(malicious_formatter)
    malicious_logger.addHandler(malicious_handler)
    malicious_logger.propagate = False
    return malicious_logger
def log_flaggedIP(logger, ip, sources, extra_data):
    logger.warning(f"Flagged DNS Query: {ip}\nSources: {sources}\n{extra_data}")

def store_ip_to_file(ip):
    global whitelisted_ips
    whitelisted_ips.add(ip)

def load_ips_from_file():
    global whitelisted_ips
    try:
        with open("clean_ips.txt", "r") as file:
            whitelisted_ips = set(line.strip() for line in file)
    except FileNotFoundError:
        with open("clean_ips.txt", "w") as file:
            whitelisted_ips = set()

def validate_ip(ip_address):
    try:
        #If IPv4?
        socket.inet_pton(socket.AF_INET, ip_address)
        return True
    except socket.error:
        pass
    
    try:
        #Is IPv6?
        socket.inet_pton(socket.AF_INET6, ip_address)
        return True
    except socket.error:
        pass
    
    return False

def virustotal_query(ip, logger):
    print(f"Checking ip: {ip}")
    if ip not in whitelisted_ips:
        result = IP_Filter(IP_Lookup(get_api(), ip))
        if result is not None:
            log_flaggedIP(logger, ip, result[0], result[1])
        elif result is None:
            store_ip_to_file(ip)

def read_dns_windows():
    logger = create_logger()
    stop_threads = threading.Event()

    def extract_dns_info(packet) -> Optional[Dict]:
        if not (packet.haslayer(DNS) and packet.haslayer(IP)):
            return None

        dns_info = {
            'query_ip': packet[IP].src,
            'response_ip': packet[IP].dst,
            'answers': []
        }

        for i in range(packet[DNS].ancount):
            rr = packet[DNS].an[i]
            if rr.type == 1:
                dns_info['answers'].append(rr.rdata)

        return dns_info

    def packet_callback(packet):
        if packet.haslayer(DNS) and packet[DNS].qr == 1:
            dns_info = extract_dns_info(packet)
            if dns_info:
                all_ips = {dns_info['query_ip'], dns_info['response_ip']}
                all_ips.update(ip for ip in dns_info['answers'] if validate_ip(str(ip)))
                
                for ip in all_ips:
                    if validate_ip(str(ip)):
                        virustotal_query(str(ip), logger)

    def sniff_interface(iface):
        try:
            sniff(iface=iface, 
                  filter="port 53", 
                  prn=packet_callback, 
                  store=0,
                  stop_filter=lambda _: stop_threads.is_set())
        except Exception:
            pass

    interfaces = [iface for iface in conf.ifaces.data.keys() 
                 if isinstance(iface, str) and 
                 'Loopback' not in iface and 
                 '\\Device\\NPF_' in iface]
    
    if not interfaces:
        return

    threads = []
    for iface in interfaces:
        thread = threading.Thread(target=sniff_interface, args=(iface,))
        thread.daemon = True
        thread.start()
        threads.append(thread)

    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        stop_threads.set()
        for thread in threads:
            thread.join(timeout=1)

def read_dns_mac(dnsmasq_log_path):
    logger = create_logger()

    with open(dnsmasq_log_path, "r") as file:
        while True:
            line = file.readline()
            if line:
                if "reply" in line:
                    #ipv4 and ipv6
                    match = re.search(r'is\s+(.*)', line)
                    ip = match.group(1)
                    if validate_ip(ip):
                        virustotal_query(ip, logger)
            else:
                time.sleep(5)



def job(os, dns_log_path):
    load_ips_from_file()
    if os == "m":
        with open(dns_log_path, "w") as file:
            file.truncate(0)
        command = ["sudo", "brew", "services", "restart", "dnsmasq"]
        subprocess.run(command, check=True)
        read_dns_mac(dns_log_path)
    if os == "w":
        read_dns_windows()

