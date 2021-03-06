#!/usr/bin/env python
# vim: ai ts=4 sts=4 et sw=4 encoding=utf-8
from __future__ import with_statement
try:
    # NOTE Python 2.5 requires installation of simplejson library
    # http://pypi.python.org/pypi/simplejson
    import simplejson as json
except ImportError:
    # Python 2.6 includes json library
    import json

from datetime import datetime
import threading
import time

from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned

import rapidsms
from ussd.models import *

"""
    Example JSON object describing a mobile network
    {
        "Country Name":"Senegal",
        "Country Code":"SN",
        "Operator Short":"ORANGE SN",
        "Operator Numeric":"60801",
        "Operator Identities": "['Orange', '+6005500']",
        "USSD Balance":"#123#",
        "USSD Transfer":"#116*1*%(destination)d*%(amount)d*%(PIN)d#",
        "Subscriber Pattern":"^(\+?221|0)(77)\d{7}$"
    }

    "Country Name" is human-friendly, "Country Code" is ISO country code,
    "Operator Short" is the human-friendly 'short alphanumeric' name returned
    by AT+COPS?, "Operator Numeric" is the 'numeric' name returned by AT+COPS
    and is globally unique (in MCC/MNC format where first 3 digits give country
    code and last two give network code), "Operator Identities" is a list of
    names or numbers the operator is identified by when sending notices and/or
    confirmation messages to users, "USSD Balance" is the USSD string
    for checking airtime balance, "USSD Transfer" is the USSD string for
    transferring airtime credit with labled string substitutions for 
    destination (phone number credit is to be sent to), amount (amount of airtime
    or currency units), and PIN, "Subscriber Pattern" is a regular expression
    that will match fully-qualified (with country code prefix) or locally-
    originated numbers for all of the operator's number blocks.
"""
class App (rapidsms.app.App):
    def start(self):
        # TODO OS agnostic!
        mobile_networks_file = 'apps/ussd/mobile_networks.json'
        with open(mobile_networks_file, 'r') as f:
            setattr(self, "mobile_networks", json.load(f))
        self.info("[transferer] Starting up...")
        transferer_interval = 10
        transferer_thread = threading.Thread(target=self.transferer_loop,\
            args=(transferer_interval,))
        transferer_thread.daemon = True
        transferer_thread.start()

    def parse(self, message):
        pass

    def _run_ussd(self, backend_slug, ussd_string):
        """ Given a backend slug and USSD string, gets backend from router
            and executes USSD string."""
        backend = self.router.get_backend(backend_slug)
        return backend._Backend__run_ussd(ussd_string)

    def _get_network_by(self, field, search):
        """ Find a network operator's JSON object by field name and value. """
        for network in self.mobile_networks:
            f = network.get(field)
            if f is not None:
                if f == search:
                    return network
        return None

    def _get_network_by_identity(self, identity):
        """ Find a network operator's JSON object by identity. """
        for network in self.mobile_networks:
            identities = network.get("Operator Identities")
            if identities is not None:
                if identity in identities:
                    return network
        return None

    def update_balances(self):
        self.debug('updating balances...')
        sims = SIM.objects.all()
        balances = {}
        for sim in sims:
            self.debug(sim.operator_name)
            b = self.check_balance(sim)
            sim.balance = b
            sim.save()
            balances.update({sim.operator_name : b})
        return balances

    def check_balance(self, sim):
        self.debug('checking balance...')
        network = self._get_network_by("Operator Short", sim.operator_name)
        if network is not None:
            result = self._run_ussd(sim.backend.slug, network["USSD Balance"])
            if result is not None:
                self.debug(result)
                notice = OperatorNotification(sim=sim, type='B', text=result,\
                    identity='USSD')
                notice.save()
                result_list = result.split()
                # return the first token that is a number and hope
                # that its the airtime balance
                for token in result_list:
                    if token.isdigit():
                        return token
                # if there is no number in the result, return the
                # whole string so it can be reviewed via the web
                return result
            return "Unknown. Please try again later."

    def recharge_airtime(self, sim):
        self.debug('recharging airtime...')
        # TODO
        pass

    def transfer_airtime(self, sim, destination, amount, pin="", force=False):
        self.debug('transferring airtime...')
        network = self._get_network_by("Operator Short", sim.operator_name)
        # messages confirming transfers can be very vauge -- often not
        # containing the intended destination -- so we will only initiate
        # a new transfer if there are no outstanding transfers expecting
        # a notification message
        # if you are impatient, you may force a new transfer in spite of
        # pending transactions -- but its probably better to change the
        # status of any pending transactions to 'unknown' instead
        if self.pending_transfer(network) is None or force:
            if network is not None:
                # TODO destination number must not include international prefix --
                # be more clever than this..
                if destination.startswith('+'):
                    return "Please try again without international prefix"

                # assemble ussd_string
                ussd_string = network["USSD Transfer"] % {'destination' : destination,\
                    'amount' : amount, 'PIN' : pin }
                # execute
                result = self._run_ussd(sim.backend.slug, ussd_string)
                self.debug('ussd executed!')
                if result is not None:
                    # TODO import result code dict from pygsm?
                    if not result.startswith('operation'):
                        self.debug(result)
                        # did we run a queued transfer?
                        trans = AirtimeTransfer.objects.filter(\
                            destination=destination, amount=amount,\
                            sim=sim, status='Q')[0]
                        if not trans:
                            trans = AirtimeTransfer(destination=destination,\
                                amount=amount, sim=sim)
                        trans.initiated = datetime.now()
                        trans.status = 'P'
                        trans.save()
                        self.debug(trans)
                    return result
        else:
            return "Please try again later."


    def handle(self, message):
        if message.text.lower().startswith("balance"):
            self.debug(self.update_balances())
        if message.text.lower().startswith("send"):
            self.debug(self.send_ro_credit())

        # if message's sender is an identity used by an operator,
        # try to process as a notification 
        network = self._get_network_by_identity(message.peer)
        if network is not None:
            return self.process_notification(message, network)

    def send_ro_credit(self):
        ''' Transfer 100 CFA credit to Rowena. '''
        sim = SIM.objects.all()[0]
        return self.transfer_airtime(sim, "772720297", "100")

    def process_notification(self, message, network):
        self.debug('processing notification...')
        self.debug(message.connection.identity)
        self.debug(message.text)

        # if the notification prefix numberings are any indication,
        # there may be thousands of kinds of notification messages...
        notice_type = 'U'

        # TODO these are Orange SN specific
        # need to experiment with more operators to know how to
        # do this sensibly
        if message.text.startswith('202'):
            # somebody sent us airtime
            notice_type = 'R'
        if message.text.startswith('2049'):
            # our transfer attempt failed
            # (not enough credit? max daily transfers?)
            notice_type = 'F'
        if message.text.startswith('201'):
            # our transfer succeeded!
            notice_type = 'S'

        sim = SIM.objects.get(operator_name=network["Operator Short"])

        notification = OperatorNotification(text=message.text,\
            identity=message.peer, type=notice_type, sim=sim)
        notification.save()

        pending = self.pending_transfer(network)
        self.debug(pending)
        if isinstance(pending, AirtimeTransfer):
            pending.notification = notification
            # mark airtime transfer with appropriate status
            if notification.type in ['U', 'S', 'F']:
                pending.status=notification.type
            else:
                pending.status='U'
            pending.save()


    def pending_transfer(self, network):
        self.debug('finding pending transfer...')
        try:
            pending_transfer = AirtimeTransfer.objects.get(\
                sim__operator_name=network["Operator Short"], status='P')
            self.debug('FOUND:')
            self.debug(pending_transfer)
            return pending_transfer
        except MultipleObjectsReturned:
            self.debug('many pending transfers!')
            return "MADNESS"
        except ObjectDoesNotExist:
            self.debug('no pending transfer')
            return None

    def wait_until_confirmation_or_timeout(self):
        # TODO only for one SIM?
        pauses = 0
        timeout = 10
        while pauses < timeout:
            if AirtimeTransfer.objects.filter(status='P').count() > 0:
                # hang out for a while bc there are outstanding transfers
                pauses += 1
                time.sleep(10)
                continue
            else:
                # no pending transfers, so give caller an OK to proceed
                return True
        # still no confirmation? mark ALL pending transfers as unknown
        AirtimeTransfer.objects.filter(status='P').update(status='U')
        # and give caller a green light
        return True

    def ajax_POST_transfer(self, params, form):
        self.debug(form)
        # form values come in as unicode courtesy of _ajax_ app
        sim = SIM.objects.get(pk=int(form['sim']))
        return self.transfer_airtime(sim, str(form['destination']),\
            str(form['amount']))

    def ajax_POST_balance(self, params, form):
        return self.update_balances()

    # Transferer Thread --------------------
    def transferer_loop(self, seconds=10):
        self.info("Starting transfer loop...")
        # pause so we don't try to run USSD codes
        # before pygsm has booted up
        time.sleep(10)
        while True:
            # look for any queued transfers
            # in the database, and execute 
            for transfer in AirtimeTransfer.objects.filter(status="Q"):
                self.info("Transferring %s to %s." % (transfer.amount, transfer.destination) )
                sim = SIM.objects.get(pk=transfer.sim.pk)
                trans = self.transfer_airtime(sim, transfer.destination, transfer.amount)
                if self.wait_until_confirmation_or_timeout():
                    continue
            # wait until it's time to check again
            time.sleep(seconds)
