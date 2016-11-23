#! /usr/bin/env python
from __future__ import print_function

import base64
import pprint
import random
import sys
import time
import copy

import btc
from jmclient.configure import jm_single, get_p2pk_vbyte, donation_address
from jmbase.support import get_log
from jmclient.support import calc_cj_fee, weighted_order_choose, choose_orders
from jmclient.wallet import estimate_tx_fee
from jmclient.podle import (generate_podle, get_podle_commitments,
                                    PoDLE, PoDLEError)
jlog = get_log()


class JMTakerError(Exception):
    pass

#Taker is now a class to do 1 coinjoin
class Taker(object):

    def __init__(self,
                 wallet,
                 mixdepth,
                 amount,
                 n_counterparties,
                 order_chooser=weighted_order_choose,
                 external_addr=None,
                 sign_method=None,
                 callbacks=None):
        self.wallet = wallet
        self.mixdepth = mixdepth
        self.cjamount = amount
        self.my_cj_addr = external_addr
        self.order_chooser = order_chooser
        self.n_counterparties = n_counterparties
        self.ignored_makers = None
        self.outputs = []
        self.cjfee_total = 0
        self.maker_txfee_contributions = 0
        self.txfee_default = 5000
        self.txid = None
        #allow custom wallet-based clients to use their own signing code;
        #currently only setting "wallet" is allowed, calls wallet.sign_tx(tx)
        self.sign_method = sign_method
        #External callers can set any of the 3 callbacks for filtering orders,
        #sending info messages to client, and action on completion.
        if callbacks:
            self.filter_orders_callback, self.taker_info_callback, self.on_finished_callback = callbacks
            if not self.taker_info_callback:
                self.taker_info_callback = self.default_taker_info_callback
            if not self.on_finished_callback:
                self.on_finished_callback = self.default_on_finished_callback
        else:
            self.filter_orders_callback = None
            self.taker_info_callback = self.default_taker_info_callback
            self.on_finished_callback = self.default_on_finished_callback

    def default_taker_info_callback(self, infotype, msg):
        jlog.debug(infotype + ":" + msg)

    def default_on_finished_callback(self, result):
        jlog.debug("Taker default on finished callback: " + str(result))

    def initialize(self, orderbook):
        """Once the daemon is active and has returned the current orderbook,
        select offers and prepare a commitment, then send it to the protocol
        to fill offers.
        """
        #reset destinations
        self.outputs = []
        if not self.filter_orderbook(orderbook):
            return (False,)
        #choose coins to spend
        if not self.prepare_my_bitcoin_data():
            return (False,)
        #Prepare a commitment
        commitment, revelation, errmsg = self.make_commitment()
        if not commitment:
            self.taker_info_callback("ABORT", errmsg)
            return (False,)
        else:
            self.taker_info_callback("INFO", errmsg)
        return (True, self.cjamount, commitment, revelation, self.orderbook)

    def filter_orderbook(self, orderbook):
        self.orderbook, self.total_cj_fee = choose_orders(
            orderbook, self.cjamount, self.n_counterparties, self.order_chooser,
            self.ignored_makers)
        if self.filter_orders_callback:
            accepted = self.filter_orders_callback([self.orderbook,
                                                    self.total_cj_fee])
            if not accepted:
                return False
        return True

    def prepare_my_bitcoin_data(self):
        """Get a coinjoin address and a change address; prepare inputs
        appropriate for this transaction"""
        if not self.my_cj_addr:
            try:
                self.my_cj_addr = self.wallet.get_external_addr(self.mixdepth + 1)
            except:
                self.taker_info_callback("ABORT", "Failed to get an address")
                return False
        self.my_change_addr = None
        if self.cjamount != 0:
            try:
                self.my_change_addr = self.wallet.get_internal_addr(self.mixdepth)
            except:
                self.taker_info_callback("ABORT", "Failed to get a change address")
                return False
        #TODO sweep, doesn't apply here
        self.total_txfee = 2 * self.txfee_default * self.n_counterparties
        total_amount = self.cjamount + self.total_cj_fee + self.total_txfee
        jlog.debug('total estimated amount spent = ' + str(total_amount))
        #adjust the required amount upwards to anticipate an increase in 
        #transaction fees after re-estimation; this is sufficiently conservative
        #to make failures unlikely while keeping the occurence of failure to
        #find sufficient utxos extremely rare. Indeed, a doubling of 'normal'
        #txfee indicates undesirable behaviour on maker side anyway.
        try:
            self.input_utxos = self.wallet.select_utxos(self.mixdepth,
                                                        total_amount)
        except Exception as e:
            self.taker_info_callback("ABORT",
                                "Unable to select sufficient coins: " + repr(e))
            return False
        self.utxos = {None: self.input_utxos.keys()}
        return True

    def receive_utxos(self, ioauth_data):
        """Triggered when the daemon returns utxo data from
        makers who responded; this is the completion of phase 1
        of the protocol
        """
        rejected_counterparties = []
        #Enough data, but need to authorize against the btc pubkey first.
        for nick, nickdata in ioauth_data.iteritems():
            utxo_list, auth_pub, cj_addr, change_addr, btc_sig, maker_pk = nickdata
            if not self.auth_counterparty(btc_sig, auth_pub, maker_pk):
                print("Counterparty encryption verification failed, aborting")
                #This counterparty must be rejected
                rejected_counterparties.append(nick)

        for rc in rejected_counterparties:
            del ioauth_data[rc]

        self.maker_utxo_data = {}

        for nick, nickdata in ioauth_data.iteritems():
            utxo_list, auth_pub, cj_addr, change_addr, btc_sig, maker_pk = nickdata
            self.utxos[nick] = utxo_list
            utxo_data = jm_single().bc_interface.query_utxo_set(self.utxos[
                nick])
            if None in utxo_data:
                jlog.debug(('ERROR outputs unconfirmed or already spent. '
                           'utxo_data={}').format(pprint.pformat(utxo_data)))
                # when internal reviewing of makers is created, add it here to
                # immediately quit; currently, the timeout thread suffices.
                continue

            #Complete maker authorization:
            #Extract the address fields from the utxos
            #Construct the Bitcoin address for the auth_pub field
            #Ensure that at least one address from utxos corresponds.
            input_addresses = [d['address'] for d in utxo_data]
            auth_address = btc.pubkey_to_address(auth_pub, get_p2pk_vbyte())
            if not auth_address in input_addresses:
                jlog.warn("ERROR maker's (" + nick + ")"
                         " authorising pubkey is not included "
                         "in the transaction: " + str(auth_address))
                #this will not be added to the transaction, so we will have
                #to recheck if we have enough
                continue

            total_input = sum([d['value'] for d in utxo_data])
            real_cjfee = calc_cj_fee(self.orderbook[nick]['ordertype'],
                                     self.orderbook[nick]['cjfee'],
                                     self.cjamount)
            change_amount = (total_input - self.cjamount -
                             self.orderbook[nick]['txfee'] + real_cjfee)

            # certain malicious and/or incompetent liquidity providers send
            # inputs totalling less than the coinjoin amount! this leads to
            # a change output of zero satoshis; this counterparty must be removed.
            if change_amount < jm_single().DUST_THRESHOLD:
                fmt = ('ERROR counterparty requires sub-dust change. nick={}'
                       'totalin={:d} cjamount={:d} change={:d}').format
                jlog.debug(fmt(nick, total_input, self.cjamount, change_amount))
                jlog.warn("Invalid change, too small, nick= " + nick)
                continue

            self.outputs.append({'address': change_addr,
                                 'value': change_amount})
            fmt = ('fee breakdown for {} totalin={:d} '
                   'cjamount={:d} txfee={:d} realcjfee={:d}').format
            jlog.debug(fmt(nick, total_input, self.cjamount, self.orderbook[
                nick]['txfee'], real_cjfee))
            self.outputs.append({'address': cj_addr, 'value': self.cjamount})
            self.cjfee_total += real_cjfee
            self.maker_txfee_contributions += self.orderbook[nick]['txfee']
            self.maker_utxo_data[nick] = utxo_data

        #Apply business logic of how many counterparties are enough:
        if len(self.maker_utxo_data.keys()) < jm_single().config.getint(
                "POLICY", "minimum_makers"):
            return (False,
                    "Not enough counterparties responded to fill, giving up")

        jlog.info('got all parts, enough to build a tx')
        self.nonrespondants = list(self.maker_utxo_data.keys())

        my_total_in = sum([va['value'] for u, va in self.input_utxos.iteritems()
                          ])
        if self.my_change_addr:
            #Estimate fee per choice of next/3/6 blocks targetting.
            estimated_fee = estimate_tx_fee(
                len(sum(self.utxos.values(), [])), len(self.outputs) + 2)
            jlog.info("Based on initial guess: " + str(self.total_txfee) +
                     ", we estimated a miner fee of: " + str(estimated_fee))
            #reset total
            self.total_txfee = estimated_fee
        my_txfee = max(self.total_txfee - self.maker_txfee_contributions, 0)
        my_change_value = (
            my_total_in - self.cjamount - self.cjfee_total - my_txfee)
        #Since we could not predict the maker's inputs, we may end up needing
        #too much such that the change value is negative or small. Note that
        #we have tried to avoid this based on over-estimating the needed amount
        #in SendPayment.create_tx(), but it is still a possibility if one maker
        #uses a *lot* of inputs.
        if self.my_change_addr and my_change_value <= 0:
            raise ValueError("Calculated transaction fee of: " + str(
                self.total_txfee) +
                             " is too large for our inputs;Please try again.")
        elif self.my_change_addr and my_change_value <= jm_single(
        ).BITCOIN_DUST_THRESHOLD:
            jlog.info("Dynamically calculated change lower than dust: " + str(
                my_change_value) + "; dropping.")
            self.my_change_addr = None
            my_change_value = 0
        jlog.info(
            'fee breakdown for me totalin=%d my_txfee=%d makers_txfee=%d cjfee_total=%d => changevalue=%d'
            % (my_total_in, my_txfee, self.maker_txfee_contributions,
               self.cjfee_total, my_change_value))
        if self.my_change_addr is None:
            if my_change_value != 0 and abs(my_change_value) != 1:
                # seems you wont always get exactly zero because of integer
                # rounding so 1 satoshi extra or fewer being spent as miner
                # fees is acceptable
                jlog.debug(('WARNING CHANGE NOT BEING '
                           'USED\nCHANGEVALUE = {}').format(my_change_value))
        else:
            self.outputs.append({'address': self.my_change_addr,
                                 'value': my_change_value})
        self.utxo_tx = [dict([('output', u)])
                        for u in sum(self.utxos.values(), [])]
        self.outputs.append({'address': self.coinjoin_address(),
                             'value': self.cjamount})
        random.shuffle(self.utxo_tx)
        random.shuffle(self.outputs)
        tx = btc.mktx(self.utxo_tx, self.outputs)
        jlog.debug('obtained tx\n' + pprint.pformat(btc.deserialize(tx)))

        self.latest_tx = btc.deserialize(tx)
        for index, ins in enumerate(self.latest_tx['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
            if utxo not in self.input_utxos.keys():
                continue
            # placeholders required
            ins['script'] = 'deadbeef'

        return (True, self.maker_utxo_data.keys(), tx)

    def auth_counterparty(self, btc_sig, auth_pub, maker_pk):
        """Validate the counterpartys claim to own the btc
        address/pubkey that will be used for coinjoining
        with an ecdsa verification.
        """
        if not btc.ecdsa_verify(maker_pk, btc_sig, auth_pub):
            jlog.debug('signature didnt match pubkey and message')
            return False
        return True

    def on_sig(self, nick, sigb64):
        sig = base64.b64decode(sigb64).encode('hex')
        inserted_sig = False
        txhex = btc.serialize(self.latest_tx)

        # batch retrieval of utxo data
        utxo = {}
        ctr = 0
        for index, ins in enumerate(self.latest_tx['ins']):
            utxo_for_checking = ins['outpoint']['hash'] + ':' + str(ins[
                'outpoint']['index'])
            if (ins['script'] != '' or
                    utxo_for_checking in self.input_utxos.keys()):
                continue
            utxo[ctr] = [index, utxo_for_checking]
            ctr += 1
        utxo_data = jm_single().bc_interface.query_utxo_set([x[
            1] for x in utxo.values()])

        # insert signatures
        for i, u in utxo.iteritems():
            if utxo_data[i] is None:
                continue
            sig_good = btc.verify_tx_input(txhex, u[0], utxo_data[i]['script'],
                                           *btc.deserialize_script(sig))
            if sig_good:
                jlog.debug('found good sig at index=%d' % (u[0]))
                self.latest_tx['ins'][u[0]]['script'] = sig
                inserted_sig = True
                # check if maker has sent everything possible
                self.utxos[nick].remove(u[1])
                if len(self.utxos[nick]) == 0:
                    jlog.debug(('nick = {} sent all sigs, removing from '
                               'nonrespondant list').format(nick))
                    self.nonrespondants.remove(nick)
                break
        if not inserted_sig:
            jlog.debug('signature did not match anything in the tx')
            # TODO what if the signature doesnt match anything
            # nothing really to do except drop it, carry on and wonder why the
            # other guy sent a failed signature

        tx_signed = True
        for ins in self.latest_tx['ins']:
            if ins['script'] == '':
                tx_signed = False
        if not tx_signed:
            return False
        assert not len(self.nonrespondants)
        jlog.debug('all makers have sent their signatures')
        self.self_sign_and_push()
        return True

    def make_commitment(self):
        """The Taker default commitment function, which uses PoDLE.
        Alternative commitment types should use a different commit type byte.
        This will allow future upgrades to provide different style commitments
        by subclassing Taker and changing the commit_type_byte; existing makers
        will simply not accept this new type of commitment.
        In case of success, return the commitment and its opening.
        In case of failure returns (None, None) and constructs a detailed
        log for the user to read and discern the reason.
        """

        def filter_by_coin_age_amt(utxos, age, amt):
            results = jm_single().bc_interface.query_utxo_set(utxos,
                                                              includeconf=True)
            newresults = []
            too_old = []
            too_small = []
            for i, r in enumerate(results):
                #results return "None" if txo is spent; drop this
                if not r:
                    continue
                valid_age = r['confirms'] >= age
                valid_amt = r['value'] >= amt
                if not valid_age:
                    too_old.append(utxos[i])
                if not valid_amt:
                    too_small.append(utxos[i])
                if valid_age and valid_amt:
                    newresults.append(utxos[i])

            return newresults, too_old, too_small

        def priv_utxo_pairs_from_utxos(utxos, age, amt):
            #returns pairs list of (priv, utxo) for each valid utxo;
            #also returns lists "too_old" and "too_small" for any
            #utxos that did not satisfy the criteria for debugging.
            priv_utxo_pairs = []
            new_utxos, too_old, too_small = filter_by_coin_age_amt(utxos.keys(),
                                                                   age, amt)
            new_utxos_dict = {k: v for k, v in utxos.items() if k in new_utxos}
            for k, v in new_utxos_dict.iteritems():
                addr = v['address']
                priv = self.wallet.get_key_from_addr(addr)
                if priv:  #can be null from create-unsigned
                    priv_utxo_pairs.append((priv, k))
            return priv_utxo_pairs, too_old, too_small

        commit_type_byte = "P"
        podle_data = None
        tries = jm_single().config.getint("POLICY", "taker_utxo_retries")
        age = jm_single().config.getint("POLICY", "taker_utxo_age")
        #Minor rounding errors don't matter here
        amt = int(self.cjamount *
                  jm_single().config.getint("POLICY",
                                            "taker_utxo_amtpercent") / 100.0)
        priv_utxo_pairs, to, ts = priv_utxo_pairs_from_utxos(self.input_utxos,
                                                             age, amt)
        #Note that we ignore the "too old" and "too small" lists in the first
        #pass through, because the same utxos appear in the whole-wallet check.

        #For podle data format see: podle.PoDLE.reveal()
        #In first round try, don't use external commitments
        podle_data = generate_podle(priv_utxo_pairs, tries)
        if not podle_data:
            #We defer to a second round to try *all* utxos in wallet;
            #this is because it's much cleaner to use the utxos involved
            #in the transaction, about to be consumed, rather than use
            #random utxos that will persist after. At this step we also
            #allow use of external utxos in the json file.
            if self.wallet.unspent:
                priv_utxo_pairs, to, ts = priv_utxo_pairs_from_utxos(
                    self.wallet.unspent, age, amt)
            #Pre-filter the set of external commitments that work for this
            #transaction according to its size and age.
            dummy, extdict = get_podle_commitments()
            if len(extdict.keys()) > 0:
                ext_valid, ext_to, ext_ts = filter_by_coin_age_amt(
                    extdict.keys(), age, amt)
            else:
                ext_valid = None
            podle_data = generate_podle(priv_utxo_pairs, tries, ext_valid)
        if podle_data:
            jlog.debug("Generated PoDLE: " + pprint.pformat(podle_data))
            revelation = PoDLE(u=podle_data['utxo'],
                                   P=podle_data['P'],
                                   P2=podle_data['P2'],
                                   s=podle_data['sig'],
                                   e=podle_data['e']).serialize_revelation()
            return (commit_type_byte + podle_data["commit"], revelation,
                    "Commitment sourced OK")
        else:
            #we know that priv_utxo_pairs all passed age and size tests, so
            #they must have failed the retries test. Summarize this info,
            #return error message to caller, and also dump to commitments_debug.txt
            errmsg = ""
            errmsgheader = ("Failed to source a commitment; this debugging information"
                      " may help:\n\n")
            errmsg += ("1: Utxos that passed age and size limits, but have "
                        "been used too many times (see taker_utxo_retries "
                        "in the config):\n")
            if len(priv_utxo_pairs) == 0:
                errmsg += ("None\n")
            else:
                for p, u in priv_utxo_pairs:
                    errmsg += (str(u) + "\n")
            errmsg += ("2: Utxos that have less than " + jm_single(
            ).config.get("POLICY", "taker_utxo_age") + " confirmations:\n")
            if len(to) == 0:
                errmsg += ("None\n")
            else:
                for t in to:
                    errmsg += (str(t) + "\n")
            errmsg += ("3: Utxos that were not at least " + \
                    jm_single().config.get(
                        "POLICY", "taker_utxo_amtpercent") + "% of the "
                    "size of the coinjoin amount " + str(
                        self.cjamount) + "\n")
            if len(ts) == 0:
                errmsg += ("None\n")
            else:
                for t in ts:
                    errmsg += (str(t) + "\n")
            errmsg += ('***\n')
            errmsg += ("Utxos that appeared in item 1 cannot be used again.\n")
            errmsg += (
                "Utxos only in item 2 can be used by waiting for more "
                "confirmations, (set by the value of taker_utxo_age).\n")
            errmsg += ("Utxos only in item 3 are not big enough for this "
                    "coinjoin transaction, set by the value "
                    "of taker_utxo_amtpercent.\n")
            errmsg += (
                "If you cannot source a utxo from your wallet according "
                "to these rules, use the tool add-utxo.py to source a "
                "utxo external to your joinmarket wallet. Read the help "
                "with 'python add-utxo.py --help'\n\n")
            errmsg += ("You can also reset the rules in the joinmarket.cfg "
                    "file, but this is generally inadvisable.\n")
            errmsg += (
                "***\nFor reference, here are the utxos in your wallet:\n")
            errmsg += ("\n" + str(self.wallet.unspent))

            with open("commitments_debug.txt", "wb") as f:
                errmsgfileheader = ("THIS IS A TEMPORARY FILE FOR DEBUGGING; "
                        "IT CAN BE SAFELY DELETED ANY TIME.\n")
                errmsgfileheader += ("***\n")
                f.write(errmsgfileheader + errmsg)

            return (None, None, errmsgheader + errmsg)

    def get_commitment(self, utxos, amount):
        """Create commitment to fulfil anti-DOS requirement of makers,
        storing the corresponding reveal/proof data for next step.
        """
        while True:
            self.commitment, self.reveal_commitment = self.make_commitment(
                self.wallet, utxos, amount)
            if (self.commitment) or (jm_single().wait_for_commitments == 0):
                break
            jlog.debug("Failed to source commitments, waiting 3 minutes")
            time.sleep(3 * 60)
        if not self.commitment:
            jlog.debug(
                "Cannot construct transaction, failed to generate "
                "commitment, shutting down. Please read commitments_debug.txt "
                "for some information on why this is, and what can be "
                "done to remedy it.")
            #TODO: would like to raw_input here to show the user, but
            #interactivity is undesirable here.
            #Test only:
            if jm_single().config.get("BLOCKCHAIN",
                                      "blockchain_source") == 'regtest':
                raise PoDLEError("For testing raising podle exception")
            #The timeout/recovery code is designed to handle non-responsive
            #counterparties, but this condition means that the current bot
            #is not able to create transactions following its *own* rules,
            #so shutting down is appropriate no matter what style
            #of bot this is.
            #These two settings shut down the timeout thread and avoid recovery.
            self.all_responded = True
            self.end_timeout_thread = True
            self.msgchan.shutdown()

    def coinjoin_address(self):
        if self.my_cj_addr:
            return self.my_cj_addr
        else:
            addr, self.sign_k = donation_address()
            return addr

    def sign_tx(self, tx, i, priv):
        if self.my_cj_addr:
            return btc.sign(tx, i, priv)
        else:
            return btc.sign(tx,
                            i,
                            priv,
                            usenonce=btc.safe_hexlify(self.sign_k))

    def self_sign(self):
        # now sign it ourselves
        tx = btc.serialize(self.latest_tx)
        if self.sign_method == "wallet":
            #Currently passes addresses of to-be-signed inputs
            #to backend wallet; this is correct for Electrum, may need
            #different info for other backends.
            addrs = {}
            for index, ins in enumerate(self.latest_tx['ins']):
                utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
                if utxo not in self.input_utxos.keys():
                    continue
                addrs[index] = self.input_utxos[utxo]['address']
            tx = self.wallet.sign_tx(btc.serialize(wallet_tx), addrs)
        else:
            for index, ins in enumerate(self.latest_tx['ins']):
                utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
                if utxo not in self.input_utxos.keys():
                    continue
                addr = self.input_utxos[utxo]['address']
                tx = self.sign_tx(tx, index, self.wallet.get_key_from_addr(addr))
        self.latest_tx = btc.deserialize(tx)

    def push(self):
        tx = btc.serialize(self.latest_tx)
        jlog.debug('\n' + tx)
        self.txid = btc.txhash(tx)
        jlog.debug('txid = ' + self.txid)
        pushed = jm_single().bc_interface.pushtx(tx)
        self.on_finished_callback(pushed)

    def self_sign_and_push(self):
        self.self_sign()
        return self.push()