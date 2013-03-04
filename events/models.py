# coding: utf-8

from django.contrib import auth
from django.db import models
from django.db import transaction
from django.utils.translation import ugettext as _

from math import exp

from bladepolska.pubnub import PubNub
from fandjango.models import User
from .exceptions import *


def round_price(price):
    return round(price, 2)


class EventManager(models.Manager):
    def ongoing_only_queryset(self):
        allowed_outcome = EVENT_OUTCOMES_DICT['IN_PROGRESS']
        return self.filter(outcome=allowed_outcome)

    def get_latest_events(self):
        return self.ongoing_only_queryset().order_by('-created_date')

    def get_featured_events(self):
        return self.ongoing_only_queryset().filter(is_featured=True).order_by('-created_date')


class BetManager(models.Manager):
    def get_users_bets_for_events(self, user, events):
        bets = self.filter(user=user, event__in=events)

        return bets

    def get_user_event_and_bet_for_update(self, user, event_id, for_outcome):
        event = list(Event.objects.select_for_update().filter(id=event_id))
        try:
            event = event[0]
        except IndexError:
            raise NonexistantEvent(_("Requested event does not exist."))

        if not event.is_in_progress:
            raise EventNotInProgress(_("Event is no longer in progress."))

        if for_outcome not in BET_OUTCOMES_DICT:
            raise UnknownOutcome()

        bet_outcome = BET_OUTCOMES_DICT[for_outcome]
        bet, created = self.get_or_create(user_id=user.id, event_id=event.id, outcome=bet_outcome)
        bet = list(self.select_for_update().filter(id=bet.id))[0]

        user = list(auth.get_user_model().objects.select_for_update().filter(id=user.id))[0]

        return user, event, bet

    @transaction.commit_on_success()
    def buy_a_bet(self, user, event_id, for_outcome, price):
        user, event, bet = self.get_user_event_and_bet_for_update(user, event_id, for_outcome)

        if for_outcome == 'YES':
            transaction_type = TRANSACTION_TYPES_DICT['BUY_YES']
        else:
            transaction_type = TRANSACTION_TYPES_DICT['BUY_NO']

        requested_price = round_price(price)
        current_tx_price = event.price_for_outcome(for_outcome, direction='BUY')
        if requested_price != current_tx_price:
            raise PriceMismatch(_("Price has changed."), event)

        quantity = 1
        bought_for_total = current_tx_price * quantity

        if (user.total_cash < bought_for_total):
            raise InsufficientCash(_("You don't have enough cash."), user)

        Transaction.objects.create(
            user_id=user.id, event_id=event.id, type=transaction_type,
            quantity=quantity, price=current_tx_price)

        event_total_bought_price = (bet.bought_avg_price * bet.bought)
        after_bought_quantity = bet.bought + quantity

        bet.bought_avg_price = (event_total_bought_price + bought_for_total) / after_bought_quantity
        bet.has += quantity
        bet.bought += quantity

        bet.save(force_update=True)

        user.total_cash -= bought_for_total
        user.save(force_update=True)

        event.increment_quantity(for_outcome, by_amount=quantity)
        event.save(force_update=True)

        # @TODO: ActivityLog

        PubNub().publish({
            'channel': event.publish_channel,
            'message': {
                'updates': {
                    'events': [event.event_dict]
                }
            }
        })

        return user, event, bet

    @transaction.commit_on_success()
    def sell_a_bet(self, user, event_id, for_outcome, price):
        user, event, bet = self.get_user_event_and_bet_for_update(user, event_id, for_outcome)

        requested_price = round_price(price)
        current_tx_price = event.price_for_outcome(for_outcome, direction='SELL')
        if requested_price != current_tx_price:
            raise PriceMismatch(_("Price has changed."), event)

        quantity = 1
        sold_for_total = current_tx_price * quantity

        if (bet.has < quantity):
            raise InsufficientBets(_("You don't have enough shares."), bet)

        if for_outcome == 'YES':
            transaction_type = TRANSACTION_TYPES_DICT['SELL_YES']
        else:
            transaction_type = TRANSACTION_TYPES_DICT['SELL_NO']
        Transaction.objects.create(
            user_id=user.id, event_id=event.id, type=transaction_type,
            quantity=quantity, price=current_tx_price)

        event_total_sold_price = (bet.sold_avg_price * bet.sold)
        after_sold_quantity = bet.sold + quantity

        bet.sold_avg_price = (event_total_sold_price + sold_for_total) / after_sold_quantity
        bet.has -= quantity
        bet.sold += quantity

        bet.save(force_update=True)

        user.total_cash += sold_for_total
        user.save(force_update=True)

        event.increment_quantity(for_outcome, by_amount=-quantity)
        event.save(force_update=True)

        # @TODO: ActivityLog

        PubNub().publish({
            'channel': event.publish_channel,
            'message': {
                'updates': {
                    'events': [event.event_dict]
                }
            }
        })

        return user, event, bet


class TransactionManager(models.Manager):
    pass

EVENT_OUTCOMES_DICT = {
    'IN_PROGRESS': 1,
    'CANCELLED': 2,
    'FINISHED_YES': 3,
    'FINISHED_NO': 4,
}

EVENT_OUTCOMES = [
    (1, 'w trakcie'),
    (2, 'anulowane'),
    (3, 'rozstrzygnięte na TAK'),
    (4, 'rozstrzygnięte na NIE'),
]


class Event(models.Model):
    objects = EventManager()

    title = models.TextField(u"tytuł wydarzenia")
    short_title = models.TextField(u"tytuł promocyjny wydarzenia")
    descrition = models.TextField(u"pełny opis wydarzenia")

    small_image = models.ImageField(u"mały obrazek", upload_to="events_small", null=True)
    big_image = models.ImageField(u"duży obrazek", upload_to="events_big", null=True)

    is_featured = models.BooleanField(u"featured", default=False)
    outcome = models.PositiveIntegerField(u"rozstrzygnięcie", choices=EVENT_OUTCOMES, default=1)

    created_date = models.DateTimeField(auto_now_add=True)
    estimated_end_date = models.DateTimeField(u"data rozstrzygnięcia")

    current_buy_for_price = models.FloatField(u"cena nabycia akcji zdarzenia", default=50.0)
    current_buy_against_price = models.FloatField(u"cena nabycia akcji zdarzenia przeciwnego", default=50.0)
    current_sell_for_price = models.FloatField(u"cena sprzedaży akcji zdarzenia", default=50.0)
    current_sell_against_price = models.FloatField(u"cena sprzedaży akcji zdarzenia przeciwnego", default=50.0)

    last_transaction_date = models.DateTimeField(u"data ostatniej transakcji", null=True)

    Q_for = models.IntegerField(u"zakładów na TAK", default=0)
    Q_against = models.IntegerField(u"zakładów na NIE", default=0)

    B = models.FloatField(u"stała B", default=5)

    @property
    def is_in_progress(self):
        return self.outcome == EVENT_OUTCOMES_DICT['IN_PROGRESS']

    @property
    def publish_channel(self):
        return "event_%d" % self.id

    @property
    def event_dict(self):
        return {
            'event_id': self.id,
            'buy_for_price': self.current_buy_for_price,
            'buy_against_price': self.current_buy_against_price,
            'sell_for_price': self.current_sell_for_price,
            'sell_against_price': self.current_sell_against_price,
        }

    def price_for_outcome(self, outcome, direction='BUY'):
        if (direction, outcome) not in BET_OUTCOMES_TO_PRICE_ATTR:
            raise UnknownOutcome()

        attr = BET_OUTCOMES_TO_PRICE_ATTR[(direction, outcome)]
        return getattr(self, attr)

    def increment_quantity(self, outcome, by_amount):
        if outcome not in BET_OUTCOMES_TO_QUANTITY_ATTR:
            raise UnknownOutcome()

        attr = BET_OUTCOMES_TO_QUANTITY_ATTR[outcome]
        setattr(self, attr, getattr(self, attr) + by_amount)

        self.recalculate_prices()

    def recalculate_prices(self):
        factor = 100.

        B = self.B

        Q_for = self.Q_for
        Q_against = self.Q_against
        Q_for_sell = max(0, Q_for - 1)
        Q_against_sell = max(0, Q_against - 1)

        e_for_buy = exp(Q_for / B)
        e_against_buy = exp(Q_against / B)
        e_for_sell = exp(Q_for_sell / B)
        e_against_sell = exp(Q_against_sell / B)

        buy_for_price = e_for_buy / (e_for_buy + e_against_buy)
        buy_against_price = e_against_buy / (e_for_buy + e_against_buy)
        sell_for_price = e_for_sell / (e_for_sell + e_against_buy)
        sell_against_price = e_against_sell / (e_for_buy + e_against_sell)

        self.current_buy_for_price = round_price(factor * buy_for_price)
        self.current_buy_against_price = round_price(factor * buy_against_price)
        self.current_sell_for_price = round_price(factor * sell_for_price)
        self.current_sell_against_price = round_price(factor * sell_against_price)

    def save(self, **kwargs):
        if not self.id:
            self.recalculate_prices()

        super(Event, self).save()


BET_OUTCOMES_DICT = {
    'YES': True,
    'NO': False,
}

BET_OUTCOMES_INV_DICT = {
    True: 'YES',
    False: 'NO',
}

BET_OUTCOMES_TO_PRICE_ATTR = {
    ('BUY', 'YES'): 'current_buy_for_price',
    ('BUY', 'NO'): 'current_buy_against_price',
    ('SELL', 'YES'): 'current_sell_for_price',
    ('SELL', 'NO'): 'current_sell_against_price'
}

BET_OUTCOMES_TO_QUANTITY_ATTR = {
    'YES': 'Q_for',
    'NO': 'Q_against'
}

BET_OUTCOMES = [
    (True, 'udziały na TAK'),
    (False, 'udziały na NIE'),
]


class Bet(models.Model):
    objects = BetManager()

    user = models.ForeignKey(User, null=False)
    event = models.ForeignKey(Event, null=False)
    outcome = models.BooleanField("zakład na TAK", choices=BET_OUTCOMES)
    has = models.PositiveIntegerField(u"posiadane zakłady", default=0, null=False)
    bought = models.PositiveIntegerField(u"kupione zakłady", default=0, null=False)
    sold = models.PositiveIntegerField(u"sprzedane zakłady", default=0, null=False)
    bought_avg_price = models.FloatField(u"kupione po średniej cenie", default=0, null=False)
    sold_avg_price = models.FloatField(u"sprzedane po średniej cenie", default=0, null=False)
    rewarded_total = models.FloatField(u"nagroda za wynik", default=0, null=False)

    @property
    def bet_dict(self):
        return {
            'bet_id': self.id,
            'event_id': self.event.id,
            'user_id': self.user.id,
            'outcome': BET_OUTCOMES_INV_DICT[self.outcome],
            'has': self.has,
            'bought': self.bought,
            'sold': self.sold,
            'bought_avg_price': self.bought_avg_price,
            'sold_avg_price': self.sold_avg_price,
            'rewarded_total': self.rewarded_total,
        }


TRANSACTION_TYPES_DICT = {
    'BUY_YES': 1,
    'SELL_YES': 2,
    'BUY_NO': 3,
    'SELL_NO': 4,
    'EVENT_CANCELLED_REFUND': 5,
    'EVENT_WON_PRIZE': 6,
}

TRANSACTION_TYPES = [
    (1, 'zakup udziałów na TAK'),
    (2, 'sprzedaż udziałów na TAK'),
    (3, 'zakup udziałów na NIE'),
    (4, 'sprzedaż udziałów na NIE'),
    (5, 'zwrot po anulowaniu wydarzenia'),
    (6, 'wygrana po rozstrzygnięciu wydarzenia'),
]


class Transaction(models.Model):
    objects = TransactionManager()

    user = models.ForeignKey(User, null=False)
    event = models.ForeignKey(Event, null=False)
    type = models.PositiveIntegerField("rodzaj transakcji", choices=TRANSACTION_TYPES, default=1)
    date = models.DateTimeField(auto_now_add=True)
    quantity = models.PositiveIntegerField(u"ilość", default=1)
    price = models.FloatField(u"cena jednostkowa", default=0, null=False)