import pandas as pd
from abc import ABC, abstractmethod

class ExchangeInterface(ABC):
    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
        """
        Retourne toujours un DataFrame avec les colonnes exactes :
        ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        """
        pass
