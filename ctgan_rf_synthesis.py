import os
import glob
import argparse
import random
import warnings
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
from datetime import datetime

from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score

from sdv.single_table import CTGANSynthesizer
from sdv.metadata import SingleTableMetadata

warnings.filterwarnings('ignore')

FEATURE_COLS = [
    'freq1', 'noise', 'max_magnitude', 'total_gain_db',
    'base_pwr_db', 'rssi', 'relpwr_db', 'avgpwr_db',
    'rssi_dbm', 'scan_type'
]

RANDOM_SEED   = 42
MAX_FILES     = 3000   # files to load for training CTGAN
ROWS_PER_FILE = 200    # max rows sampled per file (memory control)

# LOAD & PREPROCESS REAL DATA

def load_real_data(data_path: str, max_files: int = MAX_FILES,
                   rows_per_file: int = ROWS_PER_FILE) -> pd.DataFrame:
    """
    Scan data_path recursively for CSV files, extract labels from directory
    names, engineer scan_type and rssi_dbm features, and return a clean
    combined DataFrame ready for CTGAN training.
    """
    print(f"\n{'='*60}")
    print(f"STEP 1 — Loading real data from: {data_path}")
    print(f"{'='*60}")

    all_files = glob.glob(os.path.join(data_path, '**', '*.csv'), recursive=True)
    print(f"  CSV files found : {len(all_files)}")

    if len(all_files) == 0:
        raise FileNotFoundError(
            f"No CSV files found under '{data_path}'. "
            "Check your --data_path argument."
        )

    # Stratified file sampling: keep benign/malicious ratio
    benign_files    = [f for f in all_files if 'benign'    in f.lower()]
    malicious_files = [f for f in all_files if 'malicious' in f.lower()]
    print(f"  Benign files    : {len(benign_files)}")
    print(f"  Malicious files : {len(malicious_files)}")

    random.seed(RANDOM_SEED)
    n_mal = min(len(malicious_files), max_files // 2)
    n_ben = min(len(benign_files),    max_files - n_mal)

    sampled = (random.sample(benign_files,    n_ben) +
               random.sample(malicious_files, n_mal))
    random.shuffle(sampled)
    print(f"  Files sampled   : {len(sampled)} ({n_ben} benign, {n_mal} malicious)")

    frames = []
    skipped = 0

    for fpath in sampled:
        try:
            df = pd.read_csv(fpath)

            # --- Label from directory name ---
            if 'benign' in fpath.lower():
                df['label'] = 0
            else:
                df['label'] = 1

            # --- Engineered features ---
            df['scan_type'] = 1 if 'active_scan' in fpath.lower() else 0
            if 'rssi' in df.columns:
                df['rssi_dbm'] = df['rssi'] - 95

            # --- Keep only needed columns ---
            available = [c for c in FEATURE_COLS + ['label'] if c in df.columns]
            df = df[available].dropna()

            # --- Cap rows per file to control memory ---
            if len(df) > rows_per_file:
                df = df.sample(rows_per_file, random_state=RANDOM_SEED)

            frames.append(df)

        except Exception:
            skipped += 1
            continue

    if not frames:
        raise ValueError("No data loaded. Check file format / column names.")

    data = pd.concat(frames, ignore_index=True)
    print(f"  Skipped files   : {skipped}")
    print(f"  Total rows      : {len(data):,}")
    print(f"  Label balance   : {data['label'].value_counts().to_dict()}")
    print(f"  Columns         : {list(data.columns)}\n")
    return data


# STEP 2 — BUILD SDV METADATA

def build_metadata(df: pd.DataFrame) -> SingleTableMetadata:
    """
    Describe column types to SDV so CTGAN handles discrete vs continuous
    columns correctly.  'label' and 'scan_type' are categorical (discrete);
    everything else is numerical (continuous).
    """
    print(f"{'='*60}")
    print(f"STEP 2 — Building SDV metadata")
    print(f"{'='*60}")

    metadata = SingleTableMetadata()
    metadata.detect_from_dataframe(df)

    # Override discrete columns — SDV may mis-detect them as numerical
    for col in ['label', 'scan_type']:
        if col in df.columns:
            metadata.update_column(col, sdtype='categorical')

    print(f"  Metadata ready for {len(df.columns)} columns\n")
    return metadata


# STEP 3 — TRAIN CTGAN

def train_ctgan(df: pd.DataFrame, metadata: SingleTableMetadata,
                epochs: int = 200) -> CTGANSynthesizer:
    """
    Instantiate and train a CTGANSynthesizer.

    Key hyperparameters:
      - epochs        : training iterations (more = better quality, slower)
      - batch_size    : 500 is SDV default; larger = more stable gradients
      - generator_dim : size of generator hidden layers
      - discriminator_dim: size of discriminator hidden layers
    """
    print(f"{'='*60}")
    print(f"STEP 3 — Training CTGAN  ({epochs} epochs)")
    print(f"{'='*60}")
    print(f"  Training rows   : {len(df):,}")
    print(f"  Started at      : {datetime.now().strftime('%H:%M:%S')}")

    synthesizer = CTGANSynthesizer(
        metadata,
        epochs          = epochs,
        batch_size      = 500,
        generator_dim   = (256, 256),
        discriminator_dim = (256, 256),
        verbose         = True,
    )

    synthesizer.fit(df)

    print(f"\n  Training complete: {datetime.now().strftime('%H:%M:%S')}\n")
    return synthesizer


# STEP 4 — GENERATE SYNTHETIC DATA

def generate_synthetic(synthesizer: CTGANSynthesizer, n_samples: int,
                        balance: bool, real_df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate synthetic rows.

    If balance=True, generate equal numbers of benign (label=0) and
    malicious (label=1) rows regardless of the original class ratio.
    This directly addresses the ~4:1 imbalance in the passive scan subset.

    If balance=False, generate n_samples unconditionally and let the
    synthesizer reproduce the natural distribution.
    """
    print(f"{'='*60}")
    print(f"STEP 4 — Generating synthetic data")
    print(f"{'='*60}")

    if balance:
        half = n_samples // 2
        print(f"  Balanced mode   : {half} benign + {half} malicious")

        benign_syn = synthesizer.sample(
            num_rows           = half,
            condition_column   = 'label',
            condition_value    = 0,
        )
        malicious_syn = synthesizer.sample(
            num_rows           = half,
            condition_column   = 'label',
            condition_value    = 1,
        )
        synthetic = pd.concat([benign_syn, malicious_syn], ignore_index=True)
        synthetic = synthetic.sample(frac=1, random_state=RANDOM_SEED).reset_index(drop=True)

    else:
        print(f"  Unconditional   : {n_samples} rows (natural distribution)")
        synthetic = synthesizer.sample(num_rows=n_samples)

    print(f"  Generated rows  : {len(synthetic):,}")
    print(f"  Label balance   : {synthetic['label'].value_counts().to_dict()}\n")
    return synthetic


# STEP 5 — QUALITY EVALUATION

def evaluate_quality(real_df: pd.DataFrame,
                     synthetic_df: pd.DataFrame,
                     output_path: str) -> None:
    """
    Two-pronged quality check:

    A) Statistical: compare column-level distributions (mean, std, min, max)
       between real and synthetic data.

    B) Train-on-Synthetic Test-on-Real (TSTR): train a Random Forest on
       synthetic data only, test it on real held-out data, and report
       classification metrics.  A high ROC-AUC means the synthetic data
       captured the real decision boundary well.
    """
    print(f"{'='*60}")
    print(f"STEP 5 — Quality evaluation")
    print(f"{'='*60}")

    feature_cols = [c for c in FEATURE_COLS if c in real_df.columns
                    and c in synthetic_df.columns and c != 'label']

    # --- A) Statistical comparison ---
    print("\n  [A] Column statistics — real vs synthetic\n")
    stats = []
    for col in feature_cols:
        stats.append({
            'feature'   : col,
            'real_mean' : real_df[col].mean(),
            'syn_mean'  : synthetic_df[col].mean(),
            'real_std'  : real_df[col].std(),
            'syn_std'   : synthetic_df[col].std(),
            'real_min'  : real_df[col].min(),
            'syn_min'   : synthetic_df[col].min(),
            'real_max'  : real_df[col].max(),
            'syn_max'   : synthetic_df[col].max(),
        })
    stats_df = pd.DataFrame(stats).set_index('feature').round(3)
    print(stats_df.to_string())
    stats_df.to_csv(os.path.join(output_path, 'stats_comparison.csv'))

    # --- B) TSTR evaluation ---
    print("\n\n  [B] TSTR — Train on Synthetic, Test on Real\n")

    X_syn = synthetic_df[feature_cols].values
    y_syn = synthetic_df['label'].values.astype(int)

    X_real_test, _, y_real_test, _ = train_test_split(
        real_df[feature_cols].values,
        real_df['label'].values.astype(int),
        test_size=0.8,
        stratify=real_df['label'].values,
        random_state=RANDOM_SEED
    )

    clf = RandomForestClassifier(n_estimators=100, random_state=RANDOM_SEED,
                                 n_jobs=-1)
    clf.fit(X_syn, y_syn)
    y_pred  = clf.predict(X_real_test)
    y_proba = clf.predict_proba(X_real_test)[:, 1]

    print(f"  Trained on {len(X_syn):,} synthetic rows")
    print(f"  Tested  on {len(X_real_test):,} real rows\n")
    print(classification_report(y_real_test, y_pred,
                                target_names=['benign', 'malicious']))
    auc = roc_auc_score(y_real_test, y_proba)
    print(f"  ROC-AUC : {auc:.4f}")
    print(f"  (>0.85 = synthetic data is useful for training)\n")


# STEP 6 — VISUALISE DISTRIBUTIONS

def plot_distributions(real_df: pd.DataFrame,
                       synthetic_df: pd.DataFrame,
                       output_path: str) -> None:
    """
    Side-by-side KDE plots for each continuous feature.
    Real data in blue, synthetic in orange.
    Saved as distribution_comparison.png.
    """
    print(f"{'='*60}")
    print(f"STEP 6 — Plotting distributions")
    print(f"{'='*60}")

    feature_cols = [c for c in FEATURE_COLS if c in real_df.columns
                    and c not in ('label', 'scan_type')]

    n = len(feature_cols)
    fig, axes = plt.subplots(2, (n + 1) // 2, figsize=(16, 8))
    axes = axes.flatten()

    for i, col in enumerate(feature_cols):
        ax = axes[i]
        sns.kdeplot(real_df[col],      ax=ax, label='Real',      color='steelblue',  fill=True, alpha=0.4)
        sns.kdeplot(synthetic_df[col], ax=ax, label='Synthetic', color='darkorange', fill=True, alpha=0.4)
        ax.set_title(col, fontsize=11)
        ax.set_xlabel('')
        ax.legend(fontsize=8)

    for j in range(i + 1, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle('Real vs Synthetic — Feature Distributions (CTGAN)',
                 fontsize=13, fontweight='bold')
    plt.tight_layout()
    save_path = os.path.join(output_path, 'distribution_comparison.png')
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    print(f"  Saved: {save_path}\n")
    plt.close()


# STEP 7 — SAVE OUTPUTS

def save_outputs(synthesizer: CTGANSynthesizer,
                 synthetic_df: pd.DataFrame,
                 output_path: str) -> None:
    """
    Save:
      - synthetic_data.csv     : the generated rows
      - ctgan_model.pkl        : the trained synthesizer (reusable)
    """
    print(f"{'='*60}")
    print(f"STEP 7 — Saving outputs to: {output_path}")
    print(f"{'='*60}")

    os.makedirs(output_path, exist_ok=True)

    csv_path   = os.path.join(output_path, 'synthetic_data.csv')
    model_path = os.path.join(output_path, 'ctgan_model.pkl')

    synthetic_df.to_csv(csv_path, index=False)
    synthesizer.save(model_path)

    print(f"  synthetic_data.csv : {len(synthetic_df):,} rows")
    print(f"  ctgan_model.pkl    : saved (reload with CTGANSynthesizer.load())")
    print(f"\n  Done.\n")


# MAIN

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate synthetic RF jamming data using CTGAN'
    )
    p.add_argument('--data_path',   type=str, default='./data',
                   help='Root directory of the RF jamming dataset')
    p.add_argument('--output_path', type=str, default='./synthetic_output',
                   help='Directory to write outputs into')
    p.add_argument('--n_samples',   type=int, default=50000,
                   help='Number of synthetic rows to generate')
    p.add_argument('--epochs',      type=int, default=200,
                   help='CTGAN training epochs (more = better, slower)')
    p.add_argument('--max_files',   type=int, default=MAX_FILES,
                   help='Max real CSV files to load for training CTGAN')
    p.add_argument('--balance',     action='store_true',
                   help='Generate equal benign/malicious rows (fixes imbalance)')
    return p.parse_args()


def main():
    args = parse_args()
    os.makedirs(args.output_path, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  CTGAN RF JAMMING SYNTHESIZER")
    print(f"{'='*60}")
    print(f"  data_path  : {args.data_path}")
    print(f"  output     : {args.output_path}")
    print(f"  n_samples  : {args.n_samples:,}")
    print(f"  epochs     : {args.epochs}")
    print(f"  max_files  : {args.max_files}")
    print(f"  balance    : {args.balance}")

    # Pipeline
    real_df     = load_real_data(args.data_path, args.max_files)
    metadata    = build_metadata(real_df)
    synthesizer = train_ctgan(real_df, metadata, args.epochs)
    synthetic   = generate_synthetic(synthesizer, args.n_samples,
                                     args.balance, real_df)
    evaluate_quality(real_df, synthetic, args.output_path)
    plot_distributions(real_df, synthetic, args.output_path)
    save_outputs(synthesizer, synthetic, args.output_path)


if __name__ == '__main__':
    main()