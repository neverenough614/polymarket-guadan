from dotenv import load_dotenv          # Environment variable management
import os                           # Operating system interface

# Polymarket API client libraries
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    BalanceAllowanceParams,
    AssetType,
    PartialCreateOrderOptions,
    OrderMarketCancelParams,
    OrderBookSummary,
    OrderSummary,
)
from py_clob_client_v2.constants import POLYGON

# Web3 libraries for blockchain interaction
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

import requests                     # HTTP requests
import pandas as pd                 # Data analysis
import json                         # JSON processing
import subprocess                   # For calling external processes

from py_clob_client_v2.clob_types import OpenOrderParams

# Smart contract ABIs
from poly_data.abis import NegRiskAdapterABI, ConditionalTokenABI, erc20_abi

# Load environment variables
load_dotenv()


def _adapt_order_book(raw):
    if raw is None or not isinstance(raw, dict):
        return raw
    return OrderBookSummary(
        market=raw.get("market"),
        asset_id=raw.get("asset_id"),
        timestamp=raw.get("timestamp"),
        bids=[OrderSummary(price=b.get("price"), size=b.get("size")) for b in (raw.get("bids") or [])],
        asks=[OrderSummary(price=a.get("price"), size=a.get("size")) for a in (raw.get("asks") or [])],
        min_order_size=raw.get("min_order_size"),
        neg_risk=raw.get("neg_risk"),
        tick_size=raw.get("tick_size"),
        last_trade_price=raw.get("last_trade_price"),
        hash=raw.get("hash"),
    )


class PolymarketClient:
    """
    Client for interacting with Polymarket's API and smart contracts.
    
    This class provides methods for:
    - Creating and managing orders
    - Querying order book data
    - Checking balances and positions
    - Merging positions
    
    The client connects to both the Polymarket API and the Polygon blockchain.
    """
    
    def __init__(self, pk='default') -> None:
        """
        Initialize the Polymarket client with API and blockchain connections.
        
        Args:
            pk (str, optional): Private key identifier, defaults to 'default'
        """
        host="https://clob.polymarket.com"

        # Get credentials from environment variables
        key=os.getenv("PK")
        browser_address = os.getenv("BROWSER_ADDRESS")

        # Don't print sensitive wallet information
        print("Initializing Polymarket client...")
        chain_id=POLYGON
        self.browser_wallet=Web3.to_checksum_address(browser_address)

        # Initialize the Polymarket API client
        self.client = ClobClient(
            host=host,
            key=key,
            chain_id=chain_id,
            funder=self.browser_wallet,
            signature_type=2
        )

        # Set up API credentials
        self.creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds=self.creds)

        raw_get_order_book = self.client.get_order_book

        def get_order_book_v1_compat(token_id):
            return _adapt_order_book(raw_get_order_book(token_id))

        self.client.get_order_book = get_order_book_v1_compat
        
        # Initialize Web3 connection to Polygon
        web3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        # Set up USDC contract for balance checks
        self.usdc_contract = web3.eth.contract(
            address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 
            abi=erc20_abi
        )

        # Store key contract addresses
        self.addresses = {
            'neg_risk_adapter': '0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296',
            'collateral': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
            'conditional_tokens': '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045'
        }

        # Initialize contract interfaces
        self.neg_risk_adapter = web3.eth.contract(
            address=self.addresses['neg_risk_adapter'], 
            abi=NegRiskAdapterABI
        )

        self.conditional_tokens = web3.eth.contract(
            address=self.addresses['conditional_tokens'], 
            abi=ConditionalTokenABI
        )

        self.web3 = web3

    
    def create_order(self, marketId, action, price, size, neg_risk=False):
        """
        Create and submit a new order to the Polymarket order book.
        
        Args:
            marketId (str): ID of the market token to trade
            action (str): "BUY" or "SELL"
            price (float): Order price (0-1 range for prediction markets)
            size (float): Order size in USDC
            neg_risk (bool, optional): Whether this is a negative risk market. Defaults to False.
            
        Returns:
            dict: Response from the API containing order details, or empty dict on error
        """
        # Create order parameters
        order_args = OrderArgs(
            token_id=str(marketId),
            price=price,
            size=size,
            side=action
        )

        signed_order = None

        # Handle regular vs negative risk markets differently
        if neg_risk == False:
            signed_order = self.client.create_order(order_args)
        else:
            signed_order = self.client.create_order(order_args, options=PartialCreateOrderOptions(neg_risk=True))
            
        try:
            # Submit the signed order to the API
            resp = self.client.post_order(signed_order)
            return resp
        except Exception as ex:
            print(ex)
            return {}

    def get_order_book(self, market):
        """
        Get the current order book for a specific market.
        
        Args:
            market (str): Market ID to query
            
        Returns:
            tuple: (bids_df, asks_df) - DataFrames containing bid and ask orders
        """
        orderBook = self.client.get_order_book(market)
        return pd.DataFrame(orderBook.bids).astype(float), pd.DataFrame(orderBook.asks).astype(float)


    def get_usdc_balance(self):
        """
        Get the USDC balance of the connected wallet.
        
        Returns:
            float: USDC balance in decimal format
        """
        return self.usdc_contract.functions.balanceOf(self.browser_wallet).call() / 10**6
     
    def get_pos_balance(self):
        """
        Get the total value of all positions for the connected wallet.
        
        Returns:
            float: Total position value in USDC
        """
        res = requests.get(f'https://data-api.polymarket.com/value?user={self.browser_wallet}')
        return float(res.json()['value'])

    def get_total_balance(self):
        """
        Get the combined value of USDC balance and all positions.
        
        Returns:
            float: Total account value in USDC
        """
        return self.get_usdc_balance() + self.get_pos_balance()

    def get_all_positions(self) -> pd.DataFrame:
        """
        Get all positions for the connected wallet across all markets.

        API 返回形式：
          - 正常：list[dict]（多仓位）或 dict（单仓位）
          - 错误：dict 含 "error" / "message" 字段

        Returns:
            DataFrame: 持仓表。若 API 请求失败或返回错误响应，抛出异常而非
                       返回"看似正常"的 DataFrame，避免下游把错误当作"零持仓"
                       静默跳过清仓逻辑。
        """
        res = requests.get(
            f'https://data-api.polymarket.com/positions?user={self.browser_wallet}',
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
        if isinstance(data, dict):
            if "error" in data or "message" in data:
                raise RuntimeError(f"Polymarket positions API returned error: {data}")
            data = [data]  # 单仓位也是合法 dict
        return pd.DataFrame(data)
    
    def get_raw_position(self, tokenId):
        """
        Get the raw token balance for a specific market outcome token.
        
        Args:
            tokenId (int): Token ID to query
            
        Returns:
            int: Raw token amount (before decimal conversion)
        """
        return int(self.conditional_tokens.functions.balanceOf(self.browser_wallet, int(tokenId)).call())

    def get_position(self, tokenId):
        """
        Get both raw and formatted position size for a token.
        
        Args:
            tokenId (int): Token ID to query
            
        Returns:
            tuple: (raw_position, shares) - Raw token amount and decimal shares
                   Shares less than 1 are treated as 0 to avoid dust amounts
        """
        raw_position = self.get_raw_position(tokenId)
        shares = float(raw_position / 1e6)

        # Ignore very small positions (dust)
        if shares < 1:
            shares = 0

        return raw_position, shares
    
    def get_all_orders(self):
        """
        Get all open orders for the connected wallet.
        
        Returns:
            DataFrame: All open orders with their details
        """
        orders_df = pd.DataFrame(self.client.get_open_orders())

        # Convert numeric columns to float
        for col in ['original_size', 'size_matched', 'price']:
            if col in orders_df.columns:
                orders_df[col] = orders_df[col].astype(float)

        return orders_df
    
    def get_market_orders(self, market):
        """
        Get all open orders for a specific market.
        
        Args:
            market (str): Market ID to query
            
        Returns:
            DataFrame: Open orders for the specified market
        """
        orders_df = pd.DataFrame(self.client.get_open_orders(OpenOrderParams(
            market=market,
        )))

        # Convert numeric columns to float
        for col in ['original_size', 'size_matched', 'price']:
            if col in orders_df.columns:
                orders_df[col] = orders_df[col].astype(float)

        return orders_df
    

    def cancel_all_asset(self, asset_id):
        """
        Cancel all orders for a specific asset token.
        
        Args:
            asset_id (str): Asset token ID
        """
        self.client.cancel_market_orders(OrderMarketCancelParams(asset_id=str(asset_id)))


    
    def cancel_all_market(self, marketId):
        """
        Cancel all orders in a specific market.
        
        Args:
            marketId (str): Market ID
        """
        self.client.cancel_market_orders(OrderMarketCancelParams(market=marketId))

    
    def merge_positions(self, amount_to_merge, condition_id, is_neg_risk_market):
        """
        Merge positions in a market to recover collateral.
        
        This function calls the external poly_merger Node.js script to execute
        the merge operation on-chain. When you hold both YES and NO positions
        in the same market, merging them recovers your USDC.
        
        Args:
            amount_to_merge (int): Raw token amount to merge (before decimal conversion)
            condition_id (str): Market condition ID
            is_neg_risk_market (bool): Whether this is a negative risk market
            
        Returns:
            str: Transaction hash or output from the merge script
            
        Raises:
            Exception: If the merge operation fails
        """
        amount_to_merge_str = str(amount_to_merge)

        # Prepare the command to run the JavaScript script
        node_command = f'node poly_merger/merge.js {amount_to_merge_str} {condition_id} {"true" if is_neg_risk_market else "false"}'
        print(node_command)

        # Run the command and capture the output
        result = subprocess.run(node_command, shell=True, capture_output=True, text=True)
        
        # Check if there was an error
        if result.returncode != 0:
            print("Error:", result.stderr)
            raise Exception(f"Error in merging positions: {result.stderr}")
        
        print("Done merging")

        # Return the transaction hash or output
        return result.stdout
