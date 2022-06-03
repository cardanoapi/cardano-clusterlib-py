"""Tools used by `ClusterLib` for constructing transactions."""
import base64
import functools
import itertools
import logging
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Union

from cardano_clusterlib import consts
from cardano_clusterlib import exceptions
from cardano_clusterlib import structs
from cardano_clusterlib import types  # pylint: disable=unused-import

LOGGER = logging.getLogger(__name__)


def _organize_tx_ins_outs_by_coin(
    tx_list: Union[List[structs.UTXOData], List[structs.TxOut], Tuple[()]]
) -> Dict[str, list]:
    """Organize transaction inputs or outputs by coin type."""
    db: Dict[str, list] = {}
    for rec in tx_list:
        if rec.coin not in db:
            db[rec.coin] = []
        db[rec.coin].append(rec)
    return db


def _organize_utxos_by_id(tx_list: List[structs.UTXOData]) -> Dict[str, List[structs.UTXOData]]:
    """Organize UTxOs by ID (hash#ix)."""
    db: Dict[str, List[structs.UTXOData]] = {}
    for rec in tx_list:
        utxo_id = f"{rec.utxo_hash}#{rec.utxo_ix}"
        if utxo_id not in db:
            db[utxo_id] = []
        db[utxo_id].append(rec)
    return db


def _get_utxos_with_coins(
    address_utxos: List[structs.UTXOData], coins: Set[str]
) -> List[structs.UTXOData]:
    """Get all UTxOs that contain any of the required coins (`coins`)."""
    txins_by_id = _organize_utxos_by_id(address_utxos)

    txins = []
    seen_ids = set()
    for rec in address_utxos:
        utxo_id = f"{rec.utxo_hash}#{rec.utxo_ix}"
        if rec.coin in coins and utxo_id not in seen_ids:
            seen_ids.add(utxo_id)
            txins.extend(txins_by_id[utxo_id])

    return txins


def _collect_utxos_amount(
    utxos: List[structs.UTXOData], amount: int, min_change_value: int
) -> List[structs.UTXOData]:
    """Collect UTxOs so their total combined amount >= `amount`."""
    collected_utxos: List[structs.UTXOData] = []
    collected_amount = 0
    # `_min_change_value` applies only to ADA
    amount_plus_change = (
        amount + min_change_value if utxos and utxos[0].coin == consts.DEFAULT_COIN else amount
    )
    for utxo in utxos:
        # if we were able to collect exact amount, no change is needed
        if collected_amount == amount:
            break
        # make sure the change is higher than `_min_change_value`
        if collected_amount >= amount_plus_change:
            break
        collected_utxos.append(utxo)
        collected_amount += utxo.amount

    return collected_utxos


def _select_utxos(
    txins_db: Dict[str, List[structs.UTXOData]],
    txouts_passed_db: Dict[str, List[structs.TxOut]],
    txouts_mint_db: Dict[str, List[structs.TxOut]],
    fee: int,
    withdrawals: structs.OptionalTxOuts,
    min_change_value: int,
    deposit: int = 0,
) -> Set[str]:
    """Select UTxOs that can satisfy all outputs, deposits and fee.

    Return IDs of selected UTxOs.
    """
    utxo_ids: Set[str] = set()

    # iterate over coins both in txins and txouts
    for coin in set(txins_db).union(txouts_passed_db).union(txouts_mint_db):
        coin_txins = txins_db.get(coin) or []
        coin_txouts = txouts_passed_db.get(coin) or []

        # the value "-1" means all available funds
        max_index = [idx for idx, val in enumerate(coin_txouts) if val.amount == -1]
        if max_index:
            utxo_ids.update(f"{rec.utxo_hash}#{rec.utxo_ix}" for rec in coin_txins)
            continue

        total_output_amount = functools.reduce(lambda x, y: x + y.amount, coin_txouts, 0)

        if coin == consts.DEFAULT_COIN:
            tx_fee = fee if fee > 1 else 1
            funds_needed = total_output_amount + tx_fee + deposit
            total_withdrawals_amount = functools.reduce(lambda x, y: x + y.amount, withdrawals, 0)
            # fee needs an input, even if withdrawal would cover all needed funds
            input_funds_needed = max(funds_needed - total_withdrawals_amount, tx_fee)
        else:
            coin_txouts_minted = txouts_mint_db.get(coin) or []
            total_minted_amount = functools.reduce(lambda x, y: x + y.amount, coin_txouts_minted, 0)
            # In case of token burning, `total_minted_amount` might be negative.
            # Try to collect enough funds to satisfy both token burning and token
            # transfers, even though there might be an overlap.
            input_funds_needed = total_output_amount - total_minted_amount

        filtered_coin_utxos = _collect_utxos_amount(
            utxos=coin_txins, amount=input_funds_needed, min_change_value=min_change_value
        )
        utxo_ids.update(f"{rec.utxo_hash}#{rec.utxo_ix}" for rec in filtered_coin_utxos)

    return utxo_ids


def _balance_txouts(
    src_address: str,
    txouts: structs.OptionalTxOuts,
    txins_db: Dict[str, List[structs.UTXOData]],
    txouts_passed_db: Dict[str, List[structs.TxOut]],
    txouts_mint_db: Dict[str, List[structs.TxOut]],
    fee: int,
    withdrawals: structs.OptionalTxOuts,
    deposit: int = 0,
    lovelace_balanced: bool = False,
) -> List[structs.TxOut]:
    """Balance the transaction by adding change output for each coin."""
    txouts_result: List[structs.TxOut] = list(txouts)

    # iterate over coins both in txins and txouts
    for coin in set(txins_db).union(txouts_passed_db).union(txouts_mint_db):
        max_address = None
        change = 0
        coin_txins = txins_db.get(coin) or []
        coin_txouts = txouts_passed_db.get(coin) or []

        # the value "-1" means all available funds
        max_index = [idx for idx, val in enumerate(coin_txouts) if val.amount == -1]
        if len(max_index) > 1:
            raise exceptions.CLIError("Cannot send all remaining funds to more than one address.")
        if max_index:
            max_address = coin_txouts.pop(max_index[0]).address

        total_input_amount = functools.reduce(lambda x, y: x + y.amount, coin_txins, 0)
        total_output_amount = functools.reduce(lambda x, y: x + y.amount, coin_txouts, 0)

        if coin == consts.DEFAULT_COIN and lovelace_balanced:
            # balancing is done elsewhere (by the `transaction build` command)
            pass
        elif coin == consts.DEFAULT_COIN:
            tx_fee = fee if fee > 0 else 0
            total_withdrawals_amount = functools.reduce(lambda x, y: x + y.amount, withdrawals, 0)
            funds_available = total_input_amount + total_withdrawals_amount
            funds_needed = total_output_amount + tx_fee + deposit
            change = funds_available - funds_needed
            if change < 0:
                LOGGER.error(
                    "Not enough funds to make the transaction - "
                    f"available: {funds_available}; needed: {funds_needed}"
                )
        else:
            coin_txouts_minted = txouts_mint_db.get(coin) or []
            total_minted_amount = functools.reduce(lambda x, y: x + y.amount, coin_txouts_minted, 0)
            funds_available = total_input_amount + total_minted_amount
            change = funds_available - total_output_amount
            if change < 0:
                LOGGER.error(
                    f"Amount of coin `{coin}` is not sufficient - "
                    f"available: {funds_available}; needed: {total_output_amount}"
                )

        if change > 0:
            txouts_result.append(
                structs.TxOut(address=(max_address or src_address), amount=change, coin=coin)
            )

    # filter out negative amounts (tokens burning and -1 "max" amounts)
    txouts_result = [r for r in txouts_result if r.amount > 0]

    return txouts_result


def _resolve_withdrawals(
    clusterlib_obj: "types.ClusterLib", withdrawals: List[structs.TxOut]
) -> List[structs.TxOut]:
    """Return list of resolved reward withdrawals.

    The `structs.TxOut.amount` can be '-1', meaning all available funds.

    Args:
        withdrawals: A list (iterable) of `TxOuts`, specifying reward withdrawals.

    Returns:
        List[structs.TxOut]: A list of `TxOuts`, specifying resolved reward withdrawals.
    """
    resolved_withdrawals = []
    for rec in withdrawals:
        # the amount with value "-1" means all available balance
        if rec.amount == -1:
            balance = clusterlib_obj.get_stake_addr_info(rec.address).reward_account_balance
            resolved_withdrawals.append(structs.TxOut(address=rec.address, amount=balance))
        else:
            resolved_withdrawals.append(rec)

    return resolved_withdrawals


def _get_withdrawals(
    clusterlib_obj: "types.ClusterLib",
    withdrawals: structs.OptionalTxOuts,
    script_withdrawals: structs.OptionalScriptWithdrawals,
) -> Tuple[structs.OptionalTxOuts, structs.OptionalScriptWithdrawals, structs.OptionalTxOuts]:
    """Return tuple of resolved withdrawals.

    Return simple withdrawals, script withdrawals, combination of all withdrawals Tx outputs.
    """
    withdrawals = withdrawals and _resolve_withdrawals(
        clusterlib_obj=clusterlib_obj, withdrawals=withdrawals
    )
    script_withdrawals = [
        s._replace(
            txout=_resolve_withdrawals(clusterlib_obj=clusterlib_obj, withdrawals=[s.txout])[0]
        )
        for s in script_withdrawals
    ]
    withdrawals_txouts = [*withdrawals, *[s.txout for s in script_withdrawals]]
    return withdrawals, script_withdrawals, withdrawals_txouts


def _get_txout_plutus_args(txout: structs.TxOut) -> List[str]:
    txout_args = []

    # add datum arguments
    if txout.datum_hash:
        txout_args = [
            "--tx-out-datum-hash",
            str(txout.datum_hash),
        ]
    elif txout.datum_hash_file:
        txout_args = [
            "--tx-out-datum-hash-file",
            str(txout.datum_hash_file),
        ]
    elif txout.datum_hash_cbor_file:
        txout_args = [
            "--tx-out-datum-hash-cbor-file",
            str(txout.datum_hash_cbor_file),
        ]
    elif txout.datum_hash_value:
        txout_args = [
            "--tx-out-datum-hash-value",
            str(txout.datum_hash_value),
        ]
    elif txout.inline_datum_file:
        txout_args = [
            "--tx-out-inline-datum-file",
            str(txout.inline_datum_file),
        ]
    elif txout.inline_datum_cbor_file:
        txout_args = [
            "--tx-out-inline-datum-cbor-file",
            str(txout.inline_datum_cbor_file),
        ]
    elif txout.inline_datum_value:
        txout_args = [
            "--tx-out-inline-datum-value",
            str(txout.inline_datum_value),
        ]

    # add regerence spript arguments
    if txout.reference_script_file:
        txout_args.extend(
            [
                "--tx-out-reference-script-file",
                str(txout.reference_script_file),
            ]
        )

    return txout_args


def _join_txouts(txouts: List[structs.TxOut]) -> List[str]:
    txout_args: List[str] = []
    txouts_datum_order: List[str] = []
    txouts_by_datum: Dict[str, Dict[str, List[structs.TxOut]]] = {}

    # aggregate TX outputs by datum and address
    for rec in txouts:
        datum_src = str(
            rec.datum_hash
            or rec.datum_hash_file
            or rec.datum_hash_cbor_file
            or rec.datum_hash_value
            or rec.inline_datum_file
            or rec.inline_datum_cbor_file
            or rec.inline_datum_value
        )
        if datum_src not in txouts_datum_order:
            txouts_datum_order.append(datum_src)
        if datum_src not in txouts_by_datum:
            txouts_by_datum[datum_src] = {}
        txouts_by_addr = txouts_by_datum[datum_src]
        if rec.address not in txouts_by_addr:
            txouts_by_addr[rec.address] = []
        txouts_by_addr[rec.address].append(rec)

    # join txouts with the same address and datum
    for datum_src in txouts_datum_order:
        for addr, recs in txouts_by_datum[datum_src].items():
            amounts = []
            for rec in recs:
                coin = f" {rec.coin}" if rec.coin and rec.coin != consts.DEFAULT_COIN else ""
                amounts.append(f"{rec.amount}{coin}")
            amounts_joined = "+".join(amounts)

            txout_args.extend(["--tx-out", f"{addr}+{amounts_joined}"])
            txout_args.extend(_get_txout_plutus_args(txout=recs[0]))

    return txout_args


def _list_txouts(txouts: List[structs.TxOut]) -> List[str]:
    txout_args: List[str] = []

    for rec in txouts:
        txout_args.extend(["--tx-out", f"{rec.address}+{rec.amount}"])
        txout_args.extend(_get_txout_plutus_args(txout=rec))

    return txout_args


def _process_txouts(txouts: List[structs.TxOut], join_txouts: bool) -> List[str]:
    if join_txouts:
        return _join_txouts(txouts=txouts)
    return _list_txouts(txouts=txouts)


def _get_tx_ins_outs(
    clusterlib_obj: "types.ClusterLib",
    src_address: str,
    tx_files: structs.TxFiles,
    txins: structs.OptionalUTXOData = (),
    txouts: structs.OptionalTxOuts = (),
    fee: int = 0,
    deposit: Optional[int] = None,
    withdrawals: structs.OptionalTxOuts = (),
    mint_txouts: structs.OptionalTxOuts = (),
    lovelace_balanced: bool = False,
) -> Tuple[List[structs.UTXOData], List[structs.TxOut]]:
    """Return list of transaction's inputs and outputs.

    Args:
        src_address: An address used for fee and inputs (if inputs not specified by `txins`).
        tx_files: A `structs.TxFiles` tuple containing files needed for the transaction.
        txins: An iterable of `structs.UTXOData`, specifying input UTxOs (optional).
        txouts: A list (iterable) of `TxOuts`, specifying transaction outputs (optional).
        fee: A fee amount (optional).
        deposit: A deposit amount needed by the transaction (optional).
        withdrawals: A list (iterable) of `TxOuts`, specifying reward withdrawals (optional).
        mint_txouts: A list (iterable) of `TxOuts`, specifying minted tokens (optional).

    Returns:
        Tuple[list, list]: A tuple of list of transaction inputs and list of transaction
            outputs.
    """
    txouts_passed_db: Dict[str, List[structs.TxOut]] = _organize_tx_ins_outs_by_coin(txouts)
    txouts_mint_db: Dict[str, List[structs.TxOut]] = _organize_tx_ins_outs_by_coin(mint_txouts)
    outcoins_all = {consts.DEFAULT_COIN, *txouts_mint_db.keys(), *txouts_passed_db.keys()}
    outcoins_passed = [consts.DEFAULT_COIN, *txouts_passed_db.keys()]

    txins_all = list(txins) or _get_utxos_with_coins(
        address_utxos=clusterlib_obj.get_utxo(address=src_address), coins=outcoins_all
    )
    txins_db_all: Dict[str, List[structs.UTXOData]] = _organize_tx_ins_outs_by_coin(txins_all)

    tx_deposit = clusterlib_obj.get_tx_deposit(tx_files=tx_files) if deposit is None else deposit

    if not txins_all:
        LOGGER.error("No input UTxO.")
    # all output coins, except those minted by this transaction, need to be present in
    # transaction inputs
    elif not set(outcoins_passed).difference(txouts_mint_db).issubset(txins_db_all):
        LOGGER.error("Not all output coins are present in input UTxO.")

    if txins:
        # don't touch txins that were passed to the function
        txins_filtered = txins_all
        txins_db_filtered = txins_db_all
    else:
        # select only UTxOs that are needed to satisfy all outputs, deposits and fee
        selected_utxo_ids = _select_utxos(
            txins_db=txins_db_all,
            txouts_passed_db=txouts_passed_db,
            txouts_mint_db=txouts_mint_db,
            fee=fee,
            withdrawals=withdrawals,
            min_change_value=clusterlib_obj._min_change_value,
            deposit=tx_deposit,
        )
        txins_by_id: Dict[str, List[structs.UTXOData]] = _organize_utxos_by_id(txins_all)
        _txins_filtered = [utxo for uid, utxo in txins_by_id.items() if uid in selected_utxo_ids]

        txins_filtered = list(itertools.chain.from_iterable(_txins_filtered))
        txins_db_filtered = _organize_tx_ins_outs_by_coin(txins_filtered)

    if not txins_filtered:
        LOGGER.error("Cannot build transaction, empty `txins`.")

    # balance the transaction
    txouts_balanced = _balance_txouts(
        src_address=src_address,
        txouts=txouts,
        txins_db=txins_db_filtered,
        txouts_passed_db=txouts_passed_db,
        txouts_mint_db=txouts_mint_db,
        fee=fee,
        withdrawals=withdrawals,
        deposit=tx_deposit,
        lovelace_balanced=lovelace_balanced,
    )

    return txins_filtered, txouts_balanced


def get_utxo(  # noqa: C901
    utxo_dict: dict,
    address: str = "",
    coins: types.UnpackableSequence = (),
) -> List[structs.UTXOData]:
    """Return UTxO info for payment address.

    Args:
        utxo_dict: A JSON output of `query utxo`.
        address: A payment address.
        coins: A list (iterable) of coin names (asset IDs).

    Returns:
        List[structs.UTXOData]: A list of UTxO data.
    """
    utxo = []
    for utxo_rec, utxo_data in utxo_dict.items():
        utxo_hash, utxo_ix = utxo_rec.split("#")
        utxo_address = utxo_data.get("address") or ""
        addr_data = utxo_data["value"]
        datum_hash = utxo_data.get("data") or utxo_data.get("datumhash") or ""
        for policyid, coin_data in addr_data.items():
            if policyid == consts.DEFAULT_COIN:
                utxo.append(
                    structs.UTXOData(
                        utxo_hash=utxo_hash,
                        utxo_ix=int(utxo_ix),
                        amount=coin_data,
                        address=address or utxo_address,
                        coin=consts.DEFAULT_COIN,
                        datum_hash=datum_hash,
                    )
                )
                continue

            # coin data used to be a dict, now it is a list
            try:
                coin_iter = coin_data.items()
            except AttributeError:
                coin_iter = coin_data

            for asset_name, amount in coin_iter:
                decoded_coin = ""
                if asset_name:
                    try:
                        decoded_name = base64.b16decode(asset_name.encode(), casefold=True).decode(
                            "utf-8"
                        )
                        decoded_coin = f"{policyid}.{decoded_name}"
                    except Exception:
                        pass
                else:
                    decoded_coin = policyid

                utxo.append(
                    structs.UTXOData(
                        utxo_hash=utxo_hash,
                        utxo_ix=int(utxo_ix),
                        amount=amount,
                        address=address or utxo_address,
                        coin=f"{policyid}.{asset_name}" if asset_name else policyid,
                        decoded_coin=decoded_coin,
                        datum_hash=datum_hash,
                    )
                )

    if coins:
        filtered_utxo = [u for u in utxo if u.coin in coins]
        return filtered_utxo

    return utxo
