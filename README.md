<div align="center">
  <a href="https://squareslab.github.io/"><img width="auto" height="100px" src="docs/_static/images/squareslab.png"></a>
  &emsp;&emsp;&emsp;
  <a href="https://cmustrudel.github.io/"><img width="auto" height="90px" src="docs/_static/images/strudel.png"></a>
</div>

---

<div align="center">

[![GitHub Workflow Status](https://img.shields.io/github/workflow/status/qibinc/VarCLR/install)](https://github.com/qibinc/VarCLR/actions/workflows/install.yml)
[![GitHub stars](https://img.shields.io/github/stars/qibinc/VarCLR)](https://github.com/qibinc/VarCLR/stargazers)
[![GitHub license](https://img.shields.io/github/license/qibinc/VarCLR)](https://github.com/qibinc/VarCLR/blob/master/LICENSE)
[![Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/ambv/black)

</div>

## VarCLR: Variable Representation Pre-training via Contrastive Learning

- [VarCLR: Variable Representation Pre-training via Contrastive Learning](#varclr-variable-representation-pre-training-via-contrastive-learning)
  - [Step 0: Install](#step-0-install)
  - [Step 1: Load a Pre-trained VarCLR Model](#step-1-load-a-pre-trained-varclr-model)
  - [Step 2: VarCLR Variable Embeddings](#step-2-varclr-variable-embeddings)
    - [Get embedding of one variable](#get-embedding-of-one-variable)
    - [Get embeddings of list of variables (supports batching)](#get-embeddings-of-list-of-variables-supports-batching)
  - [Step 2: Get VarCLR Similarity Scores](#step-2-get-varclr-similarity-scores)
    - [Get similarity scores of N variable pairs](#get-similarity-scores-of-n-variable-pairs)
    - [Get pairwise (N * M) similarity scores from two lists of variables](#get-pairwise-n--m-similarity-scores-from-two-lists-of-variables)
  - [Step 3: Reproduce IdBench Benchmark Results](#step-3-reproduce-idbench-benchmark-results)
    - [Load the IdBench benchmark](#load-the-idbench-benchmark)
    - [Compute VarCLR scores and evaluate](#compute-varclr-scores-and-evaluate)
    - [Let's compare with the original CodeBERT](#lets-compare-with-the-original-codebert)

### Step 0: Install

```bash
pip install -e .
```

### Step 1: Load a Pre-trained VarCLR Model

```python
from varclr.models import Encoder
model = Encoder.from_pretrained("varclr-codebert")
```

### Step 2: VarCLR Variable Embeddings

#### Get embedding of one variable

```python
emb = model.encode("squareslab")
print(emb.shape)
# torch.Size([1, 768])
```

#### Get embeddings of list of variables (supports batching)

```python
emb = model.encode(["squareslab", "strudel"])
print(emb.shape)
# torch.Size([2, 768])
```

### Step 2: Get VarCLR Similarity Scores

#### Get similarity scores of N variable pairs

```python
print(model.score("squareslab", "strudel"))
# [0.42812108993530273]
print(model.score(["squareslab", "average", "max", "max"], ["strudel", "mean", "min", "maximum"]))
# [0.42812108993530273, 0.8849745988845825, 0.8035818338394165, 0.889922022819519]
```

#### Get pairwise (N * M) similarity scores from two lists of variables

```python
variable_list = ["squareslab", "strudel", "neulab"]
print(model.cross_score("squareslab", variable_list))
# [[1.0000007152557373, 0.4281214475631714, 0.7207341194152832]]
print(model.cross_score(variable_list, variable_list))
# [[1.0000007152557373, 0.4281214475631714, 0.7207341194152832],
#  [0.4281214475631714, 1.0000004768371582, 0.549992561340332],
#  [0.7207341194152832, 0.549992561340332, 1.000000238418579]]
```

### Step 3: Reproduce IdBench Benchmark Results

#### Load the IdBench benchmark

```python
from varclr.benchmarks import Benchmark

# Similarity on IdBench-Medium
b1 = Benchmark.build("idbench", variant="medium", metric="similarity")
# Relatedness on IdBench-Large
b2 = Benchmark.build("idbench", variant="large", metric="relatedness")
```

#### Compute VarCLR scores and evaluate

```python
id1_list, id2_list = b1.get_inputs()
predicted = model.score(id1_list, id2_list)
print(b1.evaluate(predicted))
# {'spearmanr': 0.5390340974574482, 'pearsonr': 0.5362089298267027}

predicted = model.score(*b2.get_inputs())
print(b2.evaluate(predicted))
# {'spearmanr': 0.7972148537584689, 'pearsonr': 0.7974362707372282}
```

#### Let's compare with the original [CodeBERT](https://github.com/microsoft/CodeBERT)

```python
codebert = Encoder.from_pretrained("codebert")
print(b1.evaluate(codebert.score(*b1.get_inputs())))
# {'spearmanr': 0.2093646019234957, 'pearsonr': 0.1776563875672398}
print(b2.evaluate(codebert.score(*b2.get_inputs())))
# {'spearmanr': 0.3901366576044072, 'pearsonr': 0.2985373327753787}
```
