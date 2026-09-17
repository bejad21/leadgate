#!/usr/bin/env python
"""
Prepare the AutoTrader cars dataset for LeadGate.

This script:
1. Downloads the rebrowser/autotrader-dataset from Kaggle
2. Combines multiple daily CSV files to get sufficient rows
3. Normalizes columns to: make, model, year, price, mileage, condition, location
4. Outputs cars_raw.csv

Column mapping (from AutoTrader dataset README):
- makeName -> make
- modelName -> model
- year -> year
- kbbFairPriceLow + kbbFairPriceHigh -> price (midpoint, since salePrice is masked as [PREMIUM])
- mileage -> mileage
- listingType -> condition (New, Used, Certified, Third-Party Certified)
- sellerCity + sellerState -> location

Target schema: make, model, year, price, mileage, condition, location
"""

import os
import sys
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
import kaggle

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")

def download_dataset():
    """Download the AutoTrader dataset from Kaggle."""
    print("Downloading rebrowser/autotrader-dataset from Kaggle...")

    # Create downloads directory
    downloads_dir = PROJECT_ROOT / "data" / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded
    csv_files = list(downloads_dir.glob("**/*.csv"))
    if csv_files:
        print(f"Dataset already downloaded ({len(csv_files)} CSV files found)")
        return downloads_dir

    # Download using kaggle API
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

def load_and_combine_csvs(downloads_dir, target_rows=350):
    """
    Load and combine CSV files to reach target row count.

    Args:
        downloads_dir: Directory containing downloaded CSV files
        target_rows: Target number of rows to sample (default 350)

    Returns:
        Combined DataFrame with all loaded rows
    """
    print("\nLoading and combining CSV files...")

    # Find all CSV files, sorted by file size (larger files first for efficiency)
    csv_files = sorted(
        downloads_dir.glob("**/*.csv"),
        key=lambda x: x.stat().st_size,
        reverse=True
    )

    if not csv_files:
        raise FileNotFoundError("No CSV files found in the downloaded dataset")

    print(f"Found {len(csv_files)} CSV file(s)")

    # Load files until we have enough rows
    dfs = []
    total_rows = 0

    for csv_file in csv_files:
        if total_rows >= target_rows:
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

    # Combine dataframes
    combined_df = pd.concat(dfs, ignore_index=True)
    print(f"\nTotal combined rows before sampling: {len(combined_df)}")

    return combined_df

def inspect_dataset(df):
    """Inspect the dataset structure."""
    print("\nDataset structure inspection:")
    print(f"Shape: {df.shape}")
    print(f"\nColumns present: {list(df.columns)[:20]}")  # Show first 20 columns
    print(f"\nKey columns check:")
    for col in ['makeName', 'modelName', 'year', 'mileage', 'listingType',
                'kbbFairPriceLow', 'kbbFairPriceHigh', 'sellerCity', 'sellerState']:
        if col in df.columns:
            print(f"  ✓ {col} - {df[col].dtype}, {df[col].notna().sum()}/{len(df)} non-null")
        else:
            print(f"  ✗ {col} - NOT FOUND")

def prepare_cars_data(df, output_path, sample_size=350):
    """
    Normalize the cars dataset to target schema.

    Args:
        df: Combined DataFrame from all CSV files
        output_path: Path to save the normalized CSV
        sample_size: Number of rows to sample (default 350, within 300-500 range)
    """
    print(f"\nPreparing data...")
    print(f"Total rows available: {len(df)}")

    # Sample rows (random sample for diversity)
    df_sample = df.sample(n=min(sample_size, len(df)), random_state=42)
    print(f"Sampled {len(df_sample)} rows")

    # Create normalized dataframe
    normalized_df = pd.DataFrame()

    # Map columns to target schema
    # make <- makeName
    if 'makeName' in df_sample.columns:
        normalized_df['make'] = df_sample['makeName'].astype(str).str.strip()
    else:
        raise ValueError("Column 'makeName' not found in dataset")

    # model <- modelName
    if 'modelName' in df_sample.columns:
        normalized_df['model'] = df_sample['modelName'].astype(str).str.strip()
    else:
        raise ValueError("Column 'modelName' not found in dataset")

    # year <- year (convert to int)
    if 'year' in df_sample.columns:
        normalized_df['year'] = pd.to_numeric(df_sample['year'], errors='coerce').astype('Int64')
    else:
        raise ValueError("Column 'year' not found in dataset")

    # price <- midpoint of kbbFairPriceLow and kbbFairPriceHigh
    # Note: salePrice is masked as [PREMIUM] in the Kaggle preview, so we use KBB values
    # which have 97% fill rate and represent fair market price estimates
    if 'kbbFairPriceLow' in df_sample.columns and 'kbbFairPriceHigh' in df_sample.columns:
        kbb_low = pd.to_numeric(df_sample['kbbFairPriceLow'], errors='coerce')
        kbb_high = pd.to_numeric(df_sample['kbbFairPriceHigh'], errors='coerce')
        price_series = ((kbb_low + kbb_high) / 2).round(0)
    else:
        # Fallback: try salePrice
        if 'salePrice' in df_sample.columns:
            price_series = pd.to_numeric(
                df_sample['salePrice'].astype(str).str.replace(r'[^\d.]', '', regex=True),
                errors='coerce'
            )
        else:
            raise ValueError("Neither KBB price columns nor salePrice found in dataset")

    normalized_df['price'] = price_series.astype('Int64')

    # mileage <- mileage (clean and convert to int)
    if 'mileage' in df_sample.columns:
        mileage_series = pd.to_numeric(
            df_sample['mileage'].astype(str).str.replace(r'[^\d.]', '', regex=True),
            errors='coerce'
        )
        normalized_df['mileage'] = mileage_series.astype('Int64')
    else:
        raise ValueError("Column 'mileage' not found in dataset")

    # condition <- listingType (New, Used, Certified, Third-Party Certified)
    if 'listingType' in df_sample.columns:
        normalized_df['condition'] = df_sample['listingType'].astype(str).str.strip()
    else:
        raise ValueError("Column 'listingType' not found in dataset")

    # location <- sellerCity + sellerState
    if 'sellerCity' in df_sample.columns and 'sellerState' in df_sample.columns:
        city = df_sample['sellerCity'].astype(str).str.strip()
        state = df_sample['sellerState'].astype(str).str.strip()
        normalized_df['location'] = (city + ', ' + state)
    elif 'sellerCity' in df_sample.columns:
        normalized_df['location'] = df_sample['sellerCity'].astype(str).str.strip()
    elif 'sellerState' in df_sample.columns:
        normalized_df['location'] = df_sample['sellerState'].astype(str).str.strip()
    else:
        # Fallback: use seller address if available
        if 'sellerAddress' in df_sample.columns:
            normalized_df['location'] = df_sample['sellerAddress'].astype(str).str.strip()
        else:
            normalized_df['location'] = 'Unknown'

    print(f"\nNormalized columns: {list(normalized_df.columns)}")
    print(f"Output shape: {normalized_df.shape}")

    # Display sample
    print(f"\nSample of normalized data:")
    print(normalized_df.head(10).to_string())

    # Save to CSV
    normalized_df.to_csv(output_path, index=False)
    print(f"\nSaved normalized data to {output_path}")

    return normalized_df

def verify_output(output_path):
    """Verify the output meets requirements."""
    print("\n" + "="*60)
    print("VERIFICATION")
    print("="*60)

    df = pd.read_csv(output_path)

    print(f"\nRow count: {df.shape[0]} (required: 300-500)")
    if 300 <= df.shape[0] <= 500:
        print("✓ Row count is within range")
    else:
        print("✗ Row count is OUT OF RANGE")

    print(f"\nNull values check (required columns):")
    for col in ['make', 'model', 'price']:
        null_count = df[col].isnull().sum()
        print(f"  {col}: {null_count} nulls", end="")
        if null_count == 0:
            print(" ✓")
        else:
            print(" ✗")

    print(f"\nColumns in output: {list(df.columns)}")
    print(f"\nData types:\n{df.dtypes}")
    print(f"\nFirst 5 rows:\n{df.head()}")
    print(f"\nDataset summary:\n{df.describe()}")
    print(f"\nValue counts by condition:\n{df['condition'].value_counts()}")

    return df

def main():
    """Main execution."""
    print("="*60)
    print("PREPARE CARS DATASET")
    print("="*60)

    # Step 1: Download dataset
    downloads_dir = download_dataset()

    # Step 2: Load and combine CSV files
    df = load_and_combine_csvs(downloads_dir, target_rows=500)

    # Step 3: Inspect dataset
    inspect_dataset(df)

    # Step 4: Prepare and normalize data
    output_path = PROJECT_ROOT / "data" / "cars_raw.csv"
    normalized_df = prepare_cars_data(df, output_path, sample_size=350)

    # Step 5: Verify output
    verify_output(output_path)

    print("\n" + "="*60)
    print("DATASET PREPARATION COMPLETE")
    print("="*60)

if __name__ == "__main__":
    main()
