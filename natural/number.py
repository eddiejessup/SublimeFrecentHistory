import locale
import re

from . import six
from .language import _

LARGE_NUMBER_SUFFIX = (
    _('thousand'),
    _('million'),
    _('billion'),
    _('trillion'),
    _('quadrillion'),
    _('quintillion'),
    _('sextillion'),
    _('septillion'),
    _('octillion'),
    _('nonillion'),
    _('decillion'),
    _('undecillion'),
    _('duodecillion'),
    _('tredecillion'),
    _('quattuordecillion'),
    _('quindecillion'),
    _('sexdecillion'),
    _('septendecillion'),
    _('octodec'),
    _('novemdecillion'),
    _('vigintillion'),
    _('unvigintillion'),
    _('duovigintil'),
    _('tresvigintillion'),
    _('quattuorvigintillion'),
    _('quinquavigintillion'),
    _('sesvigintillion'),
    _('septemvigintillion'),
    _('octovigintillion'),
    _('novemvigintillion'),
    _('trigintillion'),
    _('untrigintillion'),
    _('duotrigintillion'),
    _('trestrigintillion'),
    _('quattuortrigintillion'),
    _('quinquatrigintillion'),
    _('sestrigintillion'),
    _('septentrigintillion'),
    _('octotrigintillion'),
    _('novemtrigintillion'),
    _('quadragintillion'),
)

def _format(value, digits=None):
    if isinstance(value, six.string_types):
        value = locale.atof(value)

    number = int(value)
    convention = locale.localeconv()

    if digits is None:
        digits = convention['frac_digits']

    partials = []
    if digits == 0:
        number = int(round(value, 0))
    else:
        fraction = str(round((value - number) * 10 ** digits)).split('.')[0]
        fraction = fraction[:digits]

        if len(fraction) < digits:
            fraction = fraction.ljust(digits, '0')

        if fraction:
            partials.append(fraction)
            partials.append(convention['decimal_point'])

    number = str(number)
    for x in six.moves.xrange(len(number) + 3, 0, -3):
        partial = number[max(0, x - 3):x]
        if partial:
            partials.append(number[max(0, x - 3):x])
            partials.append(convention['thousands_sep'])

    if partials[-1] == convention['thousands_sep']:
        partials = partials[:-1]

    partials.reverse()
    return ''.join(partials)


def word(value, digits=2):
    '''
    Converts a large number to a formatted number containing the textual suffix
    for that number.

    :param value: number

    >>> print(word(1))
    1
    >>> print(word(123456789))
    123.46 million

    '''

    convention = locale.localeconv()
    decimal_point = convention['decimal_point']
    decimal_zero = re.compile(r'%s0+' % re.escape(decimal_point))
    prefix = '-' if value < 0 else ''
    value = abs(int(value))
    if value < 1000:
        return u''.join([
            prefix,
            decimal_zero.sub('', _format(value, digits)),
        ])

    for base, suffix in enumerate(LARGE_NUMBER_SUFFIX):
        exp = (base + 2) * 3
        power = 10 ** exp
        if value < power:
            value = value / float(10 ** (exp - 3))
            return ''.join([
                prefix,
                decimal_zero.sub('', _format(value, digits)),
                ' ',
                suffix,
            ])

    raise OverflowError
