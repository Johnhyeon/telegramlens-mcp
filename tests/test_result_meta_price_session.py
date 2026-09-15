"""결과 메타 v4 - price_session. 세 Lens 사본이 같은 계약을 지키는지 본다.

session 은 조회 시점의 장 상태, price_session 은 숫자가 만들어진 세션이다.
KRX 애프터마켓(2026-09-14~) 이후 둘을 한 필드로 두면 17시에 받은 가격이
애프터마켓 체결인지 정규장 종가인지 가를 수 없다.
"""

import unittest

from telegram_lens import _result_meta as rmeta


def _build(**kwargs):
    return rmeta.build_meta(lens="telegramlens", data_basis=rmeta.BASIS_LAST_CLOSE, **kwargs)


class PriceSessionContractTests(unittest.TestCase):
    def test_meta_version_is_four(self) -> None:
        self.assertEqual(rmeta.META_VERSION, 4)
        self.assertEqual(_build()["meta_v"], 4)

    def test_absent_unless_given(self) -> None:
        self.assertNotIn("price_session", _build())

    def test_undefined_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build(price_session="afterhours")

    def test_extended_values_carry_warning(self) -> None:
        for value in (
            rmeta.PRICE_SESSION_AFTER_MARKET,
            rmeta.PRICE_SESSION_PRE_MARKET,
            rmeta.PRICE_SESSION_REGULAR_AND_AFTER,
        ):
            meta = _build(price_session=value)
            self.assertEqual(meta["price_session"], value)
            self.assertEqual(meta["warnings"][0], rmeta.EXTENDED_SESSION_WARNING)

    def test_regular_and_unknown_carry_no_warning(self) -> None:
        for value in (rmeta.PRICE_SESSION_REGULAR, rmeta.PRICE_SESSION_UNKNOWN):
            self.assertNotIn(rmeta.EXTENDED_SESSION_WARNING, _build(price_session=value)["warnings"])

    def test_session_and_price_session_stay_separate(self) -> None:
        meta = _build(session="after_hours", price_session=rmeta.PRICE_SESSION_REGULAR)
        self.assertEqual((meta["session"], meta["price_session"]), ("after_hours", "regular"))

    def test_warning_is_not_duplicated(self) -> None:
        meta = _build(
            price_session=rmeta.PRICE_SESSION_AFTER_MARKET,
            warnings=[rmeta.EXTENDED_SESSION_WARNING],
        )
        self.assertEqual(meta["warnings"].count(rmeta.EXTENDED_SESSION_WARNING), 1)


if __name__ == "__main__":
    unittest.main()
