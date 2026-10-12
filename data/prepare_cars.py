#!/usr/bin/env python
"""Prepare the AutoTrader cars dataset for LeadGate.

1. Download rebrowser/autotrader-dataset from Kaggle.
2. Combine daily CSV files until there are enough candidate rows.
3. Drop rows with a null or zero price.
4. Normalize the columns to: make, model, year, price, mileage, condition, location.
5. Write cars_raw.csv: 350 sampled rows (the verification step requires 300-500).

Column mapping (from the AutoTrader dataset README):
- makeName -> make
- modelName -> model
- year -> year
- kbbFairPriceLow + kbbFairPriceHigh -> price (midpoint)
- mileage -> mileage
- listingType -> condition (New, Used, Certified, Third-Party Certified)
- sellerCity + sellerState -> location

`price` is the KBB fair-value midpoint, not the advertised price: `salePrice` is masked as
[PREMIUM] in the Kaggle preview dataset. A $0 estimate means KBB has no data for an unreleased
model, so those rows are dropped along with null ones.
"""

import os
import sys
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
import kaggle

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

def download_dataset():
    print("Downloading rebrowser/autotrader-dataset from Kaggle...")

    downloads_dir = PROJECT_ROOT / "data" / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    csv_files = list(downloads_dir.glob("**/*.csv"))
    if csv_files:
        print(f"Dataset already downloaded ({len(csv_files)} CSV files found)")
        return downloads_dir

    try:
        kaggle.api.dataset_download_files(
            'rebrowser/autotrader-dataset',
            path=downloads_dir,
            unzip=True
        )
        print(f"Dataset downloaded to {downloads_dir}")
    except Exception as e:
        print(f"Error downloading dataset: {e}")
        raise

    return downloads_dir

def load_and_combine_csvs(downloads_dir, target_candidates=1500):
    """Load and combine CSV files until at least `target_candidates` rows are loaded (1500 by default,
    extra to allow for filtering).
    """
    print("\nLoading and combining CSV files...")

    # Largest files first, so fewer files are needed to reach the target.
    csv_files = sorted(
        downloads_dir.glob("**/*.csv"),
        key=lambda x: x.stat().st_size,
        reverse=True
    )

    if not csv_files:
        raise FileNotFoundError("No CSV files found in the downloaded dataset")

    print(f"Found {len(csv_files)} CSV file(s)")

    dfs = []
    total_rows = 0

    for csv_file in csv_files:
        if total_rows >= target_candidates:
            break

        try:
            print(f"  Loading {csv_file.name} ({csv_file.stat().st_size / 1024:.1f} KB)")
            df = pd.read_csv(csv_file)
            print(f"    -> {len(df)} rows")
            dfs.append(df)
            total_rows += len(df)
        except Exception as e:
            print(f"    Error loading file: {e}")
            continue

    if not dfs:
        raise ValueError("Could not load any CSV files")

    combined_df = pd.concat(dfs, ignore_index=True)
    print(f"\nTotal combined rows before filtering: {len(combined_df)}")

    return combined_df

def inspect_dataset(df):
    print("\nDataset structure inspection:")
    print(f"Shape: {df.shape}")
    print(f"\nKey columns check:")
    for col in ['makeName', 'modelName', 'year', 'mileage', 'listingType',
                'kbbFairPriceLow', 'kbbFairPriceHigh', 'sellerCity', 'sellerState']:
        if col in df.columns:
            print(f"  [OK] {col} - {df[col].dtype}, {df[col].notna().sum()}/{len(df)} non-null")
        else:
            print(f"  [MISSING] {col} - NOT FOUND")

def prepare_cars_data(df, output_path, sample_size=350):
    """Normalize the cars dataset to the target schema, dropping rows with invalid prices.

    Samples `sample_size` rows (350 by default) and writes them to `output_path`. Raises ValueError
    if too few valid rows remain.
    """
    print(f"\nPreparing data...")
    print(f"Total rows available: {len(df)}")

    print("\nCalculating prices and filtering invalid data...")

    kbb_low = pd.to_numeric(df['kbbFairPriceLow'], errors='coerce')
    kbb_high = pd.to_numeric(df['kbbFairPriceHigh'], errors='coerce')
    calculated_price = ((kbb_low + kbb_high) / 2).round(0)

    # Bad prices: null (no KBB data at all) or zero (KBB returns 0 for unreleased models).
    bad_price_mask = (calculated_price.isnull()) | (calculated_price == 0)
    good_rows_mask = ~bad_price_mask

    bad_count = bad_price_mask.sum()
    good_count = good_rows_mask.sum()

    print(f"  Rows with null price: {(calculated_price.isnull()).sum()}")
    print(f"  Rows with $0 price: {(calculated_price == 0).sum()}")
    print(f"  Total bad prices: {bad_count}")
    print(f"  Valid rows for sampling: {good_count}")

    if good_count < sample_size:
        raise ValueError(
            f"Insufficient valid rows after filtering: {good_count} available, "
            f"but {sample_size} required for sample"
        )

    df_good = df[good_rows_mask].copy()
    calculated_price_good = calculated_price[good_rows_mask].copy()

    df_sample = df_good.sample(n=min(sample_size, len(df_good)), random_state=42)
    calculated_price_sample = calculated_price_good[df_sample.index]

    print(f"Sampled {len(df_sample)} rows from {good_count} valid rows")

    normalized_df = pd.DataFrame()

    if 'makeName' in df_sample.columns:
        normalized_df['make'] = df_sample['makeName'].astype(str).str.strip()
    else:
        raise ValueError("Column 'makeName' not found in dataset")

    if 'modelName' in df_sample.columns:
        normalized_df['model'] = df_sample['modelName'].astype(str).str.strip()
    else:
        raise ValueError("Column 'modelName' not found in dataset")

    if 'year' in df_sample.columns:
        normalized_df['year'] = pd.to_numeric(df_sample['year'], errors='coerce').astype('Int64')
    else:
        raise ValueError("Column 'year' not found in dataset")

    normalized_df['price'] = calculated_price_sample.astype('Int64')

    if 'mileage' in df_sample.columns:
        mileage_series = pd.to_numeric(
            df_sample['mileage'].astype(str).str.replace(r'[^\d.]', '', regex=True),
            errors='coerce'
        )
        normalized_df['mileage'] = mileage_series.astype('Int64')
    else:
        raise ValueError("Column 'mileage' not found in dataset")

    if 'listingType' in df_sample.columns:
        normalized_df['condition'] = df_sample['listingType'].astype(str).str.strip()
    else:
        raise ValueError("Column 'listingType' not found in dataset")

    if 'sellerCity' in df_sample.columns and 'sellerState' in df_sample.columns:
        city = df_sample['sellerCity'].astype(str).str.strip()
        state = df_sample['sellerState'].astype(str).str.strip()
        normalized_df['location'] = (city + ', ' + state)
    elif 'sellerCity' in df_sample.columns:
        normalized_df['location'] = df_sample['sellerCity'].astype(str).str.strip()
    elif 'sellerState' in df_sample.columns:
        normalized_df['location'] = df_sample['sellerState'].astype(str).str.strip()
    else:
        if 'sellerAddress' in df_sample.columns:
            normalized_df['location'] = df_sample['sellerAddress'].astype(str).str.strip()
        else:
            normalized_df['location'] = 'Unknown'

    print(f"\nNormalized columns: {list(normalized_df.columns)}")
    print(f"Output shape: {normalized_df.shape}")

    print(f"\nSample of normalized data:")
    print(normalized_df.head(10).to_string())

    normalized_df.to_csv(output_path, index=False)
    print(f"\nSaved normalized data to {output_path}")

    return normalized_df

def verify_output(output_path):
    """Check the output file: 300-500 rows, no nulls in make, model or price, and no zero prices.
    Returns True if every check passes.
    """
    print("\n" + "="*60)
    print("VERIFICATION")
    print("="*60)

    df = pd.read_csv(output_path)

    all_pass = True

    print(f"\nRow count: {df.shape[0]} (required: 300-500)")
    if 300 <= df.shape[0] <= 500:
        print("[PASS] Row count is within range")
    else:
        print("[FAIL] Row count is OUT OF RANGE")
        all_pass = False

    print(f"\nNull values check (required: ZERO):")
    for col in ['make', 'model', 'price']:
        null_count = df[col].isnull().sum()
        print(f"  {col}: {null_count} nulls", end="")
        if null_count == 0:
            print(" [PASS]")
        else:
            print(" [FAIL] VIOLATION")
            all_pass = False

    print(f"\nZero price check (required: ZERO):")
    zero_price_count = (df['price'] == 0).sum()
    print(f"  Rows with $0 price: {zero_price_count}", end="")
    if zero_price_count == 0:
        print(" [PASS]")
    else:
        print(" [FAIL] VIOLATION")
        all_pass = False

    print(f"\nColumns in output: {list(df.columns)}")
    print(f"\nData types:\n{df.dtypes}")
    print(f"\nFirst 5 rows:\n{df.head()}")
    print(f"\nDataset summary:\n{df.describe()}")
    print(f"\nValue counts by condition:\n{df['condition'].value_counts()}")

    return all_pass

def main():
    print("="*60)
    print("PREPARE CARS DATASET")
    print("="*60)

    try:
        downloads_dir = download_dataset()

        df = load_and_combine_csvs(downloads_dir, target_candidates=1500)

        inspect_dataset(df)

        output_path = PROJECT_ROOT / "data" / "cars_raw.csv"
        normalized_df = prepare_cars_data(df, output_path, sample_size=350)

        verification_passed = verify_output(output_path)

        if not verification_passed:
            raise AssertionError(
                "Output verification FAILED. Hard requirements not met. "
                "See verification output above for details."
            )

        print("\n" + "="*60)
        print("DATASET PREPARATION COMPLETE")
        print("="*60)
        print("[PASS] All verification checks passed")
        print("[PASS] Output ready for downstream tasks")

    except Exception as e:
        print(f"\n{'='*60}")
        print("ERROR DURING DATASET PREPARATION")
        print("="*60)
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
