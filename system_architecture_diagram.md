```mermaid
%%{init: {"flowchart":{"htmlLabels":true,"nodeSpacing":35,"rankSpacing":82,"curve":"linear"},"themeVariables":{"fontSize":"16px","padding":"22"}}}%%
flowchart TB
    subgraph U["User layer"]
        direction LR
        subgraph PU["Primary users<br/>Licensed spectrum"]
            direction TB
            PU1["JAZZ<br/>PU preempts SU"]
            PU2["UFONE<br/>PU preempts SU"]
        end
        subgraph SU["Secondary users<br/>CRN unlicensed"]
            direction TB
            SU1["SU node 1<br/>Opportunistic TX"]
            SU2["SU node 2<br/>Opportunistic TX"]
        end
    end

    subgraph C["Communication + sensing layer"]
        direction LR
        subgraph BS["Base station + Physical medium"]
            direction TB
            B1["OFDM modulator<br/>64 subcarriers, CP-8"]
            B2["Adaptive QAM<br/>16 / 64 / 256"]
            B3["AES-256-GCM<br/>Reed-Solomon ECC"]
            B4["MAC preemption logic"]
        end
        subgraph SS["Spectrum sensing<br/>Urkowitz detection"]
            direction TB
            SS1["I/Q sampler<br/>OFDM I/Q samples"]
            SS2["Energy envelope<br/>Detection threshold"]
        end
    end

    AE["Analytics engine<br/>Plots & Diagrams"]
    JA["Jammer agent<br/>Gaussian barrage"]

    subgraph DS["Training datasets"]
        direction TB
        KD["Kaggle RF dataset<br/>96,090 files · 3 classes"]
        CT["CTGAN synthetic RF dataset"]
    end

    subgraph ML["ML pipeline<br/>Jamming classifier"]
        direction TB
        M2["Random Forest<br/>10K files"]
        M3["XGBoost<br/>30K Files (GPU)"]
        M4["DNN<br/>96K files"]
        M5["1D-CNN<br/>96K files"]
        M6["LSTM<br/>96K files"]
    end

    PU --> BS
    SU --> BS
    BS --> AE
    SS --> AE
    JA -.-> BS
    AE --> ML
    KD --> ML
    CT --> ML

    classDef primary fill:#0B6B59,stroke:#93d6c8,color:#ffffff,stroke-width:1px;
    classDef secondary fill:#3B3A9E,stroke:#b7b6ff,color:#ffffff,stroke-width:1px;
    classDef base fill:#4B4B4B,stroke:#c9c9c9,color:#ffffff,stroke-width:1px;
    classDef sensing fill:#0C4D8C,stroke:#9ac3f0,color:#ffffff,stroke-width:1px;
    classDef analytics fill:#0E5AA3,stroke:#a9d0f2,color:#ffffff,stroke-width:1px;
    classDef jammer fill:#8A3A16,stroke:#e0b19f,color:#ffffff,stroke-width:1px;
    classDef dataset fill:#8B5A00,stroke:#e8c676,color:#ffffff,stroke-width:1px;
    classDef ml fill:#40338E,stroke:#b0a7ea,color:#ffffff,stroke-width:1px;

    class PU,PU1,PU2 primary;
    class SU,SU1,SU2 secondary;
    class BS,B1,B2,B3,B4 secondary;
    class SS,SS1,SS2 sensing;
    class AE analytics;
    class JA jammer;
    class DS,KD,CT dataset;
    class ML,M2,M3,M4,M5,M6 ml;
