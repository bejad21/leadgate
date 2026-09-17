"""
Prepare the USA Real Estate dataset for LeadGate.

This script:
1. Downloads the ahmedshahriarsakib/usa-real-estate-dataset from Kaggle
2. Filters out rows with missing core fields or invalid (null/zero) price
3. Normalizes columns to: property_type, bed, bath, price, acre_lot, city,
   state, zip_code, house_size
4. Outputs real_estate_raw.csv

Column mapping (from the raw dataset's own columns):
- price -> price (already numeric, no transformation needed)
- bed -> bed
- bath -> bath
- acre_lot -> acre_lot
- city -> city
- state -> state
- zip_code -> zip_code
- house_size -> house_size

IMPORTANT: the source dataset has NO property-type column (no house/condo/
land/apartment distinction) -- it only has a `status` field (for_sale,
ready_to_build), which is a listing/sale status, not a property type. Rather
than fabricate a type distinction the data doesn't support, `property_type`
is set to the constant "residential" for every row, since the entire
dataset is residential listings. This matches the already-known limitation
that the RealEstateAdapter's `search_listings` tool doesn't actually filter
on `property_type` yet either (see Task 2.2's review) -- there's no real
type data on either side of that gap.

Data Quality Filtering:
- Drops rows with any null value in a core field (price, bed, bath,
  acre_lot, city, state, zip_code, house_size)
- Drops rows with price <= 0 (a handful of zero/negative-price rows exist
  in the raw data and are not real listings)

Target schema: property_type, bed, bath, price, acre_lot, city, state,
zip_code, house_size
Output rows: 300-500 (sampled from pre-filtered candidates)
"""

import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

import kaggle  # noqa: E402  (must import after load_dotenv sets KAGGLE_* env vars)

DATASET_ID = "ahmedshahriarsakib/usa-real-estate-dataset"
DOWNLOAD_DIR = PROJECT_ROOT / "data" / "downloads" / "real_estate"
OUTPUT_PATH = PROJECT_ROOT / "data" / "real_estate_raw.csv"
CORE_COLUMNS = ["price", "bed", "bath", "acre_lot", "city", "state", "zip_code", "house_size"]
SAMPLE_SIZE = 350
CANDIDATE_ROWS = 100_000  # rows to read from the (2.2M-row) raw file before filtering/sampling


def download_dataset() -> Path:
    """Download the dataset from Kaggle if not already present locally."""
    if DOWNLOAD_DIR.exists() and any(DOWNLOAD_DIR.glob("*.csv")):
        print(f"Dataset already downloaded at {DOWNLOAD_DIR}, skipping download.")
        return DOWNLOAD_DIR

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    kaggle.api.authenticate()
    kaggle.api.dataset_download_files(DATASET_ID, path=str(DOWNLOAD_DIR), unzip=True)
    return DOWNLOAD_DIR


def load_candidates(download_dir: Path) -> pd.DataFrame:
    """Load a candidate pool of rows from the raw CSV (the file has 2.2M+ rows total)."""
    csv_files = list(download_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV file found in {download_dir}")

    df = pd.read_csv(csv_files[0], nrows=CANDIDATE_ROWS)
    print(f"Loaded {len(df)} candidate rows from {csv_files[0].name}")
    return df


def prepare_real_estate_data(df: pd.DataFrame, output_path: Path, sample_size: int = SAMPLE_SIZE) -> pd.DataFrame:
    """Filter, normalize, and sample the real estate dataset to the target schema."""
    print(f"\nTotal candidate rows: {len(df)}")

    missing_cols = [c for c in CORE_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Expected columns not found in dataset: {missing_cols}")

    null_mask = df[CORE_COLUMNS].isnull().any(axis=1)
    price_bad_mask = df["price"].isnull() | (df["price"] <= 0)
    bad_mask = null_mask | price_bad_mask
    good_mask = ~bad_mask

    print(f"  Rows with a null core field: {null_mask.sum()}")
    print(f"  Rows with null/<=0 price: {price_bad_mask.sum()}")
    print(f"  Valid rows for sampling: {good_mask.sum()}")

    if good_mask.sum() < sample_size:
        raise ValueError(
            f"Insufficient valid rows after filtering: {good_mask.sum()} available, "
            f"but {sample_size} required for sample. Increase CANDIDATE_ROWS."
        )

    df_good = df[good_mask].copy()
    df_sample = df_good.sample(n=min(sample_size, len(df_good)), random_state=42)

    normalized_df = pd.DataFrame(index=df_sample.index)
    normalized_df["property_type"] = pd.Series("residential", index=df_sample.index)
    normalized_df["bed"] = df_sample["bed"].astype("Int64")
    normalized_df["bath"] = df_sample["bath"].astype("Int64")
    normalized_df["price"] = df_sample["price"].round(0).astype("Int64")
    normalized_df["acre_lot"] = df_sample["acre_lot"].astype(float)
    normalized_df["city"] = df_sample["city"].astype(str)
    normalized_df["state"] = df_sample["state"].astype(str)
    normalized_df["zip_code"] = df_sample["zip_code"].astype("Int64")
    normalized_df["house_size"] = df_sample["house_size"].astype("Int64")

    normalized_df.to_csv(output_path, index=False)
    print(f"\nWrote {len(normalized_df)} rows to {output_path}")
    return normalized_df


def verify_output(output_path: Path) -> bool:
    """Verify the output meets all hard requirements. Returns True iff all checks pass."""
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    df = pd.read_csv(output_path)
    all_pass = True

    print(f"\nRow count: {df.shape[0]} (required: 300-500)")
    if 300 <= df.shape[0] <= 500:
        print("[PASS] Row count is within range")
    else:
        print("[FAIL] Row count is OUT OF RANGE")
        all_pass = False

    print("\nNull values check (required: ZERO for all target columns):")
    for col in normalized_columns():
        null_count = df[col].isnull().sum()
        print(f"  {col}: {null_count} nulls", end="")
        if null_count == 0:
            print(" [PASS]")
        else:
            print(" [FAIL] VIOLATION")
            all_pass = False

    print("\nZero/negative price check (required: ZERO):")
    bad_price_count = (df["price"] <= 0).sum()
    print(f"  Rows with price <= 0: {bad_price_count}", end="")
    if bad_price_count == 0:
        print(" [PASS]")
    else:
        print(" [FAIL] VIOLATION")
        all_pass = False

    print(f"\nColumns in output: {list(df.columns)}")
    print(f"\nDataset summary:\n{df.describe(include='all')}")

    return all_pass


def normalized_columns() -> list[str]:
    return ["property_type", "bed", "bath", "price", "acre_lot", "city", "state", "zip_code", "house_size"]


def main() -> None:
    print("=" * 60)
    print("PREPARE REAL ESTATE DATASET")
    print("=" * 60)

    try:
        download_dir = download_dataset()
        df = load_candidates(download_dir)
        prepare_real_estate_data(df, OUTPUT_PATH, sample_size=SAMPLE_SIZE)
        verification_passed = verify_output(OUTPUT_PATH)

        if not verification_passed:
            raise AssertionError(
                "Output verification FAILED. Hard requirements not met. "
                "See verification output above for details."
            )

        print("\n" + "=" * 60)
        print("DATASET PREPARATION COMPLETE")
        print("=" * 60)
        print("[PASS] All verification checks passed")

    except Exception as e:
        print("\n" + "=" * 60)
        print("ERROR DURING DATASET PREPARATION")
        print("=" * 60)
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
